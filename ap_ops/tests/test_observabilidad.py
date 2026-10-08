"""Tests del eje ops: plugin de auditoria, tabla de recomendaciones y OTel.

CERO llamadas al modelo. Los hooks se ejercitan con contextos falsos
(SimpleNamespace), que es suficiente porque el plugin solo lee atributos.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ap_ops import config
from ap_ops.esquemas import SenalOps, Severidad
from ap_ops.herramientas import analizar_proveedor_contrato, repositorio
from ap_ops.observabilidad.otel import configurar_otel, modo_desde_entorno
from ap_ops.plugins import PLANTILLAS, PluginAuditoriaAP, recomendar
from ap_ops.plugins.auditoria import _seguro

INV = "inv-1"


def correr(coro):
    return asyncio.run(coro)


def ctx(agente="validador_tributario", intento=1, id_factura="F-1"):
    return SimpleNamespace(
        invocation_id=INV,
        agent_name=agente,
        attempt_count=intento,
        state={"id_factura": id_factura},
    )


def senal(tipo, sev=Severidad.MEDIA, comp="x"):
    return SenalOps(tipo=tipo, severidad=sev, componente=comp, detalle="d", invocation_id=INV)


class TestTablaDeRecomendaciones:
    def test_todo_tipo_de_senal_tiene_plantilla(self):
        """Si se agrega un TipoSenal sin plantilla, una senal real caeria en
        'desconocida' y perderia su accion concreta."""
        from typing import get_args

        from ap_ops.esquemas import TipoSenal

        assert set(get_args(TipoSenal)) == set(PLANTILLAS)

    @pytest.mark.parametrize("tipo", sorted(PLANTILLAS))
    def test_toda_recomendacion_es_accionable(self, tipo):
        r = recomendar(senal(tipo, comp="validador_x"))
        assert r.accion_recomendada and "{" not in r.accion_recomendada
        assert r.runbook.startswith("docs/runbooks/")
        assert r.tipo_accion != "ninguna"

    @pytest.mark.parametrize("tipo", sorted(PLANTILLAS))
    def test_todo_runbook_referenciado_existe(self, tipo):
        """Una accion que remite a un archivo inexistente no es accionable."""
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[2]
        r = recomendar(senal(tipo))
        assert (raiz / r.runbook).is_file(), f"falta {r.runbook}"

    def test_la_accion_nombra_el_componente(self):
        r = recomendar(senal("error_herramienta", comp="analizar_x@validador_y"))
        assert "analizar_x@validador_y" in r.accion_recomendada

    def test_desacuerdo_lo_resuelve_negocio_no_ingenieria(self):
        """Un desacuerdo de estimadores no lo arregla un ingeniero: lo revisa un
        analista de cuentas por pagar."""
        r = recomendar(senal("desacuerdo_validadores"))
        assert r.responsable == "negocio_cxp"
        assert r.tipo_accion == "marcar_para_revision_humana"

    def test_severidad_alta_sube_la_urgencia(self):
        r = recomendar(senal("latencia_alta", Severidad.CRITICA))
        assert r.urgencia == "alta"

    def test_tipo_desconocido_no_explota(self):
        s = senal("error_herramienta")
        s = s.model_copy(update={"tipo": "algo_nuevo"})
        assert recomendar(s).tipo_accion == "abrir_incidente"


class TestPluginAuditoria:
    def test_tool_error_devuelve_instruccion_segura_y_recomendacion(self):
        p = PluginAuditoriaAP()
        tool = SimpleNamespace(name="analizar_proveedor_contrato")
        out = correr(
            p.on_tool_error_callback(
                tool=tool, tool_args={}, tool_context=ctx(), error=OSError("maestro caido")
            )
        )
        assert out["datos_disponibles"] is False
        assert "NO INVENTES" in out["instruccion_para_el_agente"]
        recs = p._registros[INV].recomendaciones_ops
        assert [r.senal.tipo for r in recs] == ["error_herramienta"]
        assert recs[0].tipo_accion == "revisar_fixture"
        assert p._registros[INV].errores_herramienta == 1

    def test_tormenta_de_reintentos(self):
        p = PluginAuditoriaAP()
        tool = SimpleNamespace(name="t")
        correr(
            p.on_tool_error_callback(
                tool=tool, tool_args={}, tool_context=ctx(intento=3), error=OSError("x")
            )
        )
        tipos = {r.senal.tipo for r in p._registros[INV].recomendaciones_ops}
        assert tipos == {"error_herramienta", "tormenta_reintentos"}

    def test_senales_repetidas_se_deduplican(self):
        """Un reintento dispara el mismo hook varias veces; diez recomendaciones
        identicas entierran la unica que importa."""
        p = PluginAuditoriaAP()
        tool = SimpleNamespace(name="t")
        for _ in range(5):
            correr(
                p.on_tool_error_callback(
                    tool=tool, tool_args={}, tool_context=ctx(), error=OSError("x")
                )
            )
        assert len(p._registros[INV].recomendaciones_ops) == 1
        assert p._registros[INV].errores_herramienta == 5

    def test_acumula_llamadas_y_tokens(self):
        p = PluginAuditoriaAP()
        um = SimpleNamespace(prompt_token_count=1000, candidates_token_count=200)
        for _ in range(3):
            correr(p.before_model_callback(callback_context=ctx(), llm_request=None))
            correr(
                p.after_model_callback(
                    callback_context=ctx(), llm_response=SimpleNamespace(usage_metadata=um)
                )
            )
        r = p._registros[INV]
        assert (r.llamadas_llm, r.tokens_entrada, r.tokens_salida) == (3, 3000, 600)
        assert len(r.latencias_por_nodo) == 3

    def test_latencia_sobre_presupuesto_emite_senal(self, monkeypatch):
        monkeypatch.setitem(config.PRESUPUESTO_LATENCIA_S, "validador_tributario", -1.0)
        p = PluginAuditoriaAP()
        correr(p.before_model_callback(callback_context=ctx(), llm_request=None))
        correr(
            p.after_model_callback(
                callback_context=ctx(), llm_response=SimpleNamespace(usage_metadata=None)
            )
        )
        tipos = [r.senal.tipo for r in p._registros[INV].recomendaciones_ops]
        assert tipos == ["latencia_alta"]

    def test_error_de_modelo_transitorio_vs_grave(self):
        p = PluginAuditoriaAP()
        correr(
            p.on_model_error_callback(
                callback_context=ctx("a"), llm_request=None, error=Exception("503 UNAVAILABLE")
            )
        )
        correr(
            p.on_model_error_callback(
                callback_context=ctx("b"), llm_request=None, error=Exception("boom")
            )
        )
        sev = {r.senal.componente: r.senal.severidad for r in p._registros[INV].recomendaciones_ops}
        assert sev == {"a": Severidad.ALTA, "b": Severidad.CRITICA}

    def test_on_event_detecta_desacuerdo_y_baja_confianza(self):
        p = PluginAuditoriaAP()
        ev = SimpleNamespace(
            author="agente_scoring_riesgo",
            output={
                "puntaje_riesgo": 15,
                "puntaje_determinista": 70,
                "desacuerdo_con_determinista": False,
                "confianza": 0.4,
            },
        )
        correr(p.on_event_callback(invocation_context=SimpleNamespace(invocation_id=INV), event=ev))
        tipos = {r.senal.tipo for r in p._registros[INV].recomendaciones_ops}
        assert tipos == {"desacuerdo_validadores", "decision_baja_confianza"}

    def test_on_event_detecta_validador_no_evaluable_y_ciclo_agotado(self):
        p = PluginAuditoriaAP()
        ic = SimpleNamespace(invocation_id=INV)
        correr(
            p.on_event_callback(
                invocation_context=ic,
                event=SimpleNamespace(
                    author="v", output={"estado": "no_evaluable", "validador": "validador_x"}
                ),
            )
        )
        correr(
            p.on_event_callback(
                invocation_context=ic,
                event=SimpleNamespace(
                    author="compuerta_critico",
                    output={"ciclo_agotado": True, "aprobado": False, "iteracion": 2},
                ),
            )
        )
        tipos = {r.senal.tipo for r in p._registros[INV].recomendaciones_ops}
        assert tipos == {"validador_no_evaluable", "ciclo_critico_agotado"}

    def test_evento_sin_diccionario_se_ignora(self):
        p = PluginAuditoriaAP()
        ev = SimpleNamespace(author="x", output="texto")
        assert correr(
            p.on_event_callback(invocation_context=SimpleNamespace(invocation_id=INV), event=ev)
        ) is None
        assert INV not in p._registros

    def test_after_run_cierra_el_registro(self):
        p = PluginAuditoriaAP()
        correr(p.before_run_callback(invocation_context=SimpleNamespace(invocation_id=INV)))
        ic = SimpleNamespace(
            invocation_id=INV,
            artifact_service=None,
            session=SimpleNamespace(state={"decision": {"resultado": "retener"}}),
        )
        correr(p.after_run_callback(invocation_context=ic))
        assert INV not in p._registros
        assert p.cerrados[0].decision_final == "retener"
        assert p.cerrados[0].fin

    def test_gasto_de_tokens_sobre_presupuesto(self):
        p = PluginAuditoriaAP()
        p._registro(INV).tokens_entrada = config.PRESUPUESTO_TOKENS_FACTURA + 1
        ic = SimpleNamespace(
            invocation_id=INV, artifact_service=None, session=SimpleNamespace(state={})
        )
        correr(p.after_run_callback(invocation_context=ic))
        tipos = [r.senal.tipo for r in p.cerrados[0].recomendaciones_ops]
        assert tipos == ["gasto_tokens_alto"]


class TestElPluginNoPuedeTumbarElPipeline:
    def test_un_hook_que_explota_devuelve_none(self):
        """REGLA DE ORO: un plugin de observabilidad no puede ser fuente de
        fallas. Si la auditoria explota, el pipeline sigue."""

        class Roto:
            @_seguro
            async def hook(self):
                raise RuntimeError("auditoria rota")

        assert correr(Roto().hook()) is None

    def test_contexto_malformado_no_propaga(self):
        p = PluginAuditoriaAP()
        # Sin invocation_id: AttributeError adentro del hook.
        assert correr(p.before_run_callback(invocation_context=SimpleNamespace())) is None
        assert correr(p.after_model_callback(callback_context=None, llm_response=None)) is None


class TestPruebaNegativaDelEjeOps:
    """El punto del eje ops: una falla de infraestructura no inventa datos.

    Es la prueba del plan, ejecutable sin LLM: el maestro de proveedores no se
    puede leer -> la herramienta lo reporta como falla de infraestructura (no
    como hallazgo de negocio) -> el validador debe responder no_evaluable.
    """

    def test_maestro_ausente_es_falla_de_infra_no_hallazgo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repositorio, "DIR_DATOS", tmp_path)
        monkeypatch.setattr(repositorio, "DIR_FACTURAS", repositorio.DIR_FACTURAS)
        repositorio.limpiar_cache()

        r = analizar_proveedor_contrato("factura_conforme")
        assert r["datos_disponibles"] is False
        assert "maestro_proveedores.json" in r["error"]
        assert r["desviaciones"] == [], "no debe inventar hallazgos"

    def test_y_el_plugin_lo_convierte_en_accion(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repositorio, "DIR_DATOS", tmp_path)
        repositorio.limpiar_cache()
        try:
            analizar_proveedor_contrato("factura_conforme")
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
        assert error is None, "la herramienta captura DatoNoDisponible; no debe propagarla"

        p = PluginAuditoriaAP()
        out = correr(
            p.on_tool_error_callback(
                tool=SimpleNamespace(name="analizar_proveedor_contrato"),
                tool_args={},
                tool_context=ctx("validador_proveedor_contrato"),
                error=repositorio.DatoNoDisponible("No existe el archivo de datos: maestro_proveedores.json"),
            )
        )
        rec = p._registros[INV].recomendaciones_ops[0]
        assert rec.tipo_accion == "revisar_fixture"
        assert rec.responsable == "ingenieria_agentes"
        assert "maestro_proveedores.json" in rec.evidencia
        assert "NO INVENTES" in out["instruccion_para_el_agente"]


class TestOtel:
    def test_por_defecto_es_noop(self, monkeypatch):
        monkeypatch.delenv("AP_OPS_OTEL", raising=False)
        assert modo_desde_entorno() == "ninguno"
        assert configurar_otel() == "ninguno"

    def test_modo_invalido_degrada_sin_levantar(self):
        assert configurar_otel("inventado") == "ninguno"

    def test_otlp_sin_endpoint_degrada(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        assert configurar_otel("otlp") == "ninguno"

    def test_cloud_sin_credenciales_degrada(self, monkeypatch):
        """La observabilidad no puede ser la razon por la que no arranca."""
        import google.auth

        def sin_creds(*a, **k):
            raise google.auth.exceptions.DefaultCredentialsError("sin ADC")

        monkeypatch.setattr(google.auth, "default", sin_creds)
        assert configurar_otel("cloud") == "ninguno"


class TestIntegracionConLosEntryPoints:
    def test_ambos_apps_registran_el_plugin(self):
        from ap_ops.agent import app as app_chat
        from ap_ops_pipeline.agent import app as app_pipeline

        for a in (app_chat, app_pipeline):
            assert any(isinstance(p, PluginAuditoriaAP) for p in a.plugins), a.name

    def test_cada_app_tiene_su_propia_instancia(self):
        """Comparten clase, no estado: dos Apps con el mismo plugin mezclarian
        los registros de invocaciones distintas."""
        from ap_ops.agent import app as a1
        from ap_ops_pipeline.agent import app as a2

        assert a1.plugins[0] is not a2.plugins[0]
