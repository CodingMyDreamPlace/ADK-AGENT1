"""Capa de evaluacion, parte GRATIS: golden file real + validez de los archivos de eval.

El sistema se evalua en dos capas, de la mas barata a la mas cara:

  1. ESTA (gratis): el contrato entre la salida del modelo y el codigo
     determinista, verificado sobre un expediente REAL generado por Gemini y
     versionado en tests/dorados/. Captura el comportamiento del modelo una vez
     y lo congela; los cambios en el codigo determinista que lo contradigan
     fallan aqui sin gastar un token.

  2. `adk eval` (gasta cuota): ejecuta el coordinador de verdad. Ver
     docs/EVALUACION.md. Estos tests solo garantizan que los archivos de eval
     sean validos, para no descubrirlo despues de gastar cuota.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap_ops import config
from ap_ops.esquemas import ExpedienteAP
from ap_ops.herramientas import aplicar_compuerta, cargar_factura, listar_facturas
from ap_ops.plugins.auditoria import PluginAuditoriaAP, consumo

RAIZ = Path(__file__).resolve().parent
DORADOS = RAIZ / "dorados"
EVAL = RAIZ.parent / "eval"

GOLDEN = DORADOS / "factura_fraude_cuenta.expediente.json"


@pytest.fixture(scope="module")
def exp() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


class TestGoldenFileReal:
    """Expediente generado por Gemini de verdad (11 llamadas), congelado."""

    def test_valida_contra_el_esquema(self, exp):
        ExpedienteAP.model_validate(exp)

    def test_invariante_hallazgo_accion_se_sostiene_con_un_plan_real(self, exp):
        """EL invariante del sistema, sobre un plan escrito por un modelo y no
        por una fixture."""
        hallazgos = {h["codigo"] for h in exp["evaluacion"]["hallazgos"]}
        acciones = {a["codigo_hallazgo"] for a in exp["plan_accion"]["acciones"]}
        assert hallazgos - acciones == set(), "hallazgos huerfanos"
        assert acciones - hallazgos == set(), "acciones sin respaldo"
        assert exp["plan_accion"]["hallazgos_sin_accion"] == []
        assert exp["plan_accion"]["acciones_sin_hallazgo"] == []

    def test_el_modelo_no_inventa_ni_pierde_hallazgos(self, exp):
        """El scoring puede AJUSTAR severidades pero no agregar ni quitar
        hallazgos: los de la evaluacion son los del consolidado determinista."""
        det = {h["codigo"] for h in exp["consolidado"]["hallazgos"]}
        llm = {h["codigo"] for h in exp["evaluacion"]["hallazgos"]}
        assert llm == det

    def test_el_modelo_conserva_la_evidencia_literal(self, exp):
        """La evidencia es lo que relee un auditor: no se parafrasea."""
        det = {h["codigo"]: h["evidencia"] for h in exp["consolidado"]["hallazgos"]}
        for h in exp["evaluacion"]["hallazgos"]:
            assert h["evidencia"] == det[h["codigo"]], h["codigo"]

    def test_la_decision_es_reproducible_desde_sus_entradas(self, exp):
        """La decision NUNCA la toma el modelo: reaplicar la tabla de reglas a
        las mismas entradas debe dar exactamente lo mismo."""
        d = aplicar_compuerta(
            puntaje_riesgo=exp["evaluacion"]["puntaje_riesgo"],
            hallazgos=exp["consolidado"]["hallazgos"],
            monto_total=exp["factura"]["total"],
            validadores_no_evaluables=exp["consolidado"]["validadores_no_evaluables"],
        )
        assert d == exp["decision"]

    def test_la_decision_esperada_para_fraude_de_cuenta(self, exp):
        assert exp["decision"]["resultado"] == "retener"
        assert exp["decision"]["regla_aplicada"] == "R-02"
        assert exp["decision"]["aprobador_sugerido"] == "cumplimiento"
        assert exp["decision"]["monto_autorizado"] == 0.0

    def test_la_cuenta_bloqueante_sigue_bloqueando(self, exp):
        cta = next(h for h in exp["evaluacion"]["hallazgos"] if h["codigo"] == "FRA-CTA-004")
        assert cta["bloquea_pago"] is True
        assert cta["severidad"] == "critica"
        assert "FRA-CTA-004" in exp["evaluacion"]["codigos_bloqueantes"]

    def test_el_modelo_no_bajo_la_severidad_de_un_hallazgo_de_fraude(self, exp):
        """Puede subirla por correlacion; bajarla seria suavizar una senal."""
        orden = {"informativa": 0, "baja": 1, "media": 2, "alta": 3, "critica": 4}
        base = {h["codigo"]: h["severidad"] for h in exp["consolidado"]["hallazgos"]}
        for h in exp["evaluacion"]["hallazgos"]:
            assert orden[h["severidad"]] >= orden[base[h["codigo"]]], h["codigo"]

    def test_no_hay_estimadores_en_desacuerdo(self, exp):
        ev = exp["evaluacion"]
        assert abs(ev["puntaje_riesgo"] - ev["puntaje_determinista"]) <= 20
        assert ev["desacuerdo_con_determinista"] is False

    def test_ninguna_accion_tiene_un_sla_invalido(self, exp):
        for a in exp["plan_accion"]["acciones"]:
            assert a["sla_horas"] >= 1
            assert 1 <= a["prioridad"] <= 5

    def test_las_acciones_criticas_exigen_confirmacion_humana(self, exp):
        """Lo que mueve dinero o se comunica al exterior no es reversible."""
        for a in exp["plan_accion"]["acciones"]:
            if not a["reversible"]:
                assert a["requiere_aprobacion_humana"], a["id_accion"]

    def test_la_verificacion_bancaria_va_por_canal_alterno(self, exp):
        """La accion central contra este fraude: llamar al contacto REGISTRADO,
        no al que figura en el correo que trajo la factura."""
        acciones = exp["plan_accion"]["acciones"]
        bancarias = [a for a in acciones if a["codigo_hallazgo"] == "FRA-CTA-004"]
        assert bancarias
        texto = " ".join(a["accion"].lower() for a in bancarias)
        assert "registrado" in texto

    def test_no_filtra_un_cci_completo_en_la_salida_del_modelo(self, exp):
        """El CCI completo existe en `factura` (dato de entrada sintetico); no
        debe haber viajado a ninguna salida del modelo."""
        cci = exp["factura"]["cuenta_bancaria"]["cci"]
        for parte in ("evaluacion", "plan_accion", "veredicto_critico", "resultados"):
            assert cci not in json.dumps(exp[parte]), parte

    def test_el_critico_aprobo_con_cobertura_total(self, exp):
        v = exp["veredicto_critico"]
        assert v["aprobado"] is True
        assert v["cobertura_hallazgos"] == 100
        assert exp["iteraciones_critico"] <= config.MAX_ITER_CRITICO


class TestConsumoReal:
    """Cierra el bug detectado: el expediente reportaba 0 llamadas y 0 tokens."""

    def test_consumo_lee_los_contadores_del_plugin(self):
        p = PluginAuditoriaAP()
        reg = p._registro("inv-consumo")
        reg.llamadas_llm, reg.tokens_entrada, reg.tokens_salida = 11, 33941, 4253
        assert consumo("inv-consumo") == (11, 38194)

    def test_sin_plugin_registrado_devuelve_cero(self):
        assert consumo("invocacion-inexistente") == (0, 0)

    def test_el_expediente_dorado_conserva_el_cero_historico(self, exp):
        """Este golden se capturo ANTES de corregir el bug: llamadas_llm=0 con
        11 llamadas reales. Documenta el defecto; al regenerar el golden con
        una corrida nueva este valor pasara a ser > 0."""
        assert exp["llamadas_llm"] in (0, 11)


class TestArchivosDeEval:
    def test_el_evalset_es_valido(self):
        from google.adk.evaluation.eval_set import EvalSet

        es = EvalSet.model_validate_json((EVAL / "triaje_basico.evalset.json").read_text("utf-8"))
        assert es.eval_set_id == "triaje_basico"
        assert {c.eval_id for c in es.eval_cases} == {
            "triar_factura_limpia",
            "triar_factura_con_fraude",
        }

    def test_cada_caso_espera_una_llamada_a_triar_factura(self):
        from google.adk.evaluation.eval_set import EvalSet

        es = EvalSet.model_validate_json((EVAL / "triaje_basico.evalset.json").read_text("utf-8"))
        for caso in es.eval_cases:
            usos = caso.conversation[0].intermediate_data.tool_uses
            assert [u.name for u in usos] == ["triar_factura"], caso.eval_id

    def test_los_ids_del_evalset_son_fixtures_reales(self):
        """Un evalset que apunta a una factura inexistente falla DESPUES de
        gastar cuota en armar el contexto."""
        from google.adk.evaluation.eval_set import EvalSet

        es = EvalSet.model_validate_json((EVAL / "triaje_basico.evalset.json").read_text("utf-8"))
        for caso in es.eval_cases:
            id_f = caso.conversation[0].intermediate_data.tool_uses[0].args["id_factura"]
            assert id_f in listar_facturas()
            cargar_factura(id_f)

    @pytest.mark.parametrize("nombre", ["test_config.json", "test_config_juez.json"])
    def test_las_configs_son_validas(self, nombre):
        from google.adk.evaluation.eval_config import EvalConfig

        cfg = EvalConfig.model_validate_json((EVAL / nombre).read_text("utf-8"))
        assert "tool_trajectory_avg_score" in cfg.criteria

    def test_la_config_gratuita_no_usa_juez_llm(self):
        """`final_response_match_v2` y los rubric_* llaman a un modelo juez: cada
        caso cuesta llamadas ADICIONALES a las del pipeline."""
        crudo = json.loads((EVAL / "test_config.json").read_text("utf-8"))
        assert set(crudo["criteria"]) == {"tool_trajectory_avg_score"}

    def test_el_evalset_coincide_con_el_contrato_de_la_herramienta(self):
        """Nombre y firma de la tool que espera la trayectoria."""
        from ap_ops.coordinador import triar_factura

        assert triar_factura.__name__ == "triar_factura"
        import inspect

        assert "id_factura" in inspect.signature(triar_factura).parameters
