"""Tests de la compuerta de decision: el unico lugar que decide si el dinero se mueve.

Estos tests son el contrato de politica del sistema. Cualquier cambio en el
orden o las condiciones de R-01..R-08 los rompe, y eso es intencional: una
decision de politica no deberia poder cambiarse sin que alguien lo note.
"""

from __future__ import annotations

import pytest

from ap_ops import config
from ap_ops.esquemas import DecisionAP, Severidad
from ap_ops.herramientas import REGLAS, aplicar_compuerta
from ap_ops.herramientas.catalogo import (
    CODIGOS_BLOQUEANTES,
    CODIGOS_INVESTIGACION,
    CODIGOS_RECHAZO,
    severidad_base,
)
from ap_ops.herramientas.scoring import hallazgo_desde_desviacion


def h(codigo: str, **extra) -> dict:
    """Construye un hallazgo minimo con la severidad base del catalogo."""
    base = {
        "codigo": codigo,
        "titulo": codigo,
        "severidad": severidad_base(codigo).value,
        "categoria": "tributario",
        "campo_afectado": "x",
        "evidencia": "e",
        "fuente": "herramienta",
        "confianza": 1.0,
        "monto_impactado": 0.0,
        "bloquea_pago": codigo in CODIGOS_BLOQUEANTES,
    }
    base.update(extra)
    return base


class TestOrdenDeLasReglas:
    def test_r01_datos_no_verificables_gana_sobre_todo(self):
        d = aplicar_compuerta(
            puntaje_riesgo=0,
            hallazgos=[h("FRA-DUP-001", categoria="duplicado_fraude")],
            monto_total=100.0,
            validadores_no_evaluables=["tributario"],
        )
        assert d["regla_aplicada"] == "R-01"
        assert d["resultado"] == "retener"

    def test_r02_investigacion_gana_sobre_r03_rechazo(self):
        """LA decision de politica central: una sospecha de fraude se investiga,
        no se rechaza. Rechazar castigaria al proveedor por un ataque que sufrio
        la empresa, y cerraria el caso sin buscar un patron mas amplio."""
        d = aplicar_compuerta(
            puntaje_riesgo=90,
            hallazgos=[
                h("FRA-CTA-004", categoria="duplicado_fraude"),
                h("FRA-DUP-001", categoria="duplicado_fraude"),
            ],
            monto_total=9950.0,
        )
        assert d["regla_aplicada"] == "R-02"
        assert d["resultado"] == "retener"
        assert d["aprobador_sugerido"] == "cumplimiento"

    def test_r03_rechaza_solo_error_material_confirmado(self):
        d = aplicar_compuerta(
            puntaje_riesgo=50,
            hallazgos=[h("FRA-DUP-001", categoria="duplicado_fraude")],
            monto_total=4720.0,
        )
        assert d["regla_aplicada"] == "R-03"
        assert d["resultado"] == "rechazar"
        assert d["monto_autorizado"] == 0.0

    def test_r04_bloqueante_no_critico_retiene_pero_es_subsanable(self):
        d = aplicar_compuerta(
            puntaje_riesgo=30,
            hallazgos=[h("TRV-OC-001", categoria="tres_vias")],
            monto_total=3540.0,
        )
        assert d["regla_aplicada"] == "R-04"
        assert d["resultado"] == "retener"
        assert d["condiciones"], "una retencion subsanable debe decir que subsanar"

    def test_r05_puntaje_alto_sin_bloqueantes(self):
        d = aplicar_compuerta(
            puntaje_riesgo=config.UMBRAL_RETENER,
            hallazgos=[h("TRI-IGV-001"), h("TRI-PER-005"), h("TRI-DET-006")],
            monto_total=1000.0,
        )
        assert d["regla_aplicada"] == "R-05"
        assert d["resultado"] == "retener"
        assert d["umbral_usado"] == float(config.UMBRAL_RETENER)

    def test_r06_puntaje_medio_aprueba_con_condiciones(self):
        d = aplicar_compuerta(
            puntaje_riesgo=30, hallazgos=[h("TRI-IGV-001")], monto_total=1000.0
        )
        assert d["regla_aplicada"] == "R-06"
        assert d["resultado"] == "aprobar_con_condiciones"
        assert d["monto_autorizado"] == 1000.0

    def test_r07_factura_limpia_pero_monto_alto(self):
        """Una factura perfecta de S/ 80.000 igual la mira alguien."""
        d = aplicar_compuerta(puntaje_riesgo=0, hallazgos=[], monto_total=80000.0)
        assert d["regla_aplicada"] == "R-07"
        assert d["resultado"] == "aprobar_con_condiciones"
        assert d["requiere_confirmacion_humana"] is True
        assert d["umbral_usado"] == config.UMBRAL_MONTO_HITL

    def test_r08_touchless_es_el_unico_sin_confirmacion_humana(self):
        d = aplicar_compuerta(puntaje_riesgo=0, hallazgos=[], monto_total=4720.0)
        assert d["regla_aplicada"] == "R-08"
        assert d["resultado"] == "touchless_approve"
        assert d["requiere_confirmacion_humana"] is False


class TestInvariantesDePolitica:
    @pytest.mark.parametrize("codigo", sorted(CODIGOS_BLOQUEANTES))
    def test_ningun_codigo_bloqueante_permite_aprobar(self, codigo):
        """El invariante mas importante: si un codigo bloquea el pago, ninguna
        regla puede resolver en una aprobacion. Se prueba contra TODOS los
        codigos bloqueantes, no contra una muestra."""
        d = aplicar_compuerta(
            puntaje_riesgo=0,
            hallazgos=[h(codigo, categoria="duplicado_fraude" if codigo.startswith("FRA") else "tributario")],
            monto_total=100.0,
        )
        assert d["resultado"] in ("retener", "rechazar"), codigo
        assert d["monto_autorizado"] == 0.0, codigo

    @pytest.mark.parametrize("codigo", sorted(CODIGOS_INVESTIGACION))
    def test_investigacion_siempre_escala_a_cumplimiento(self, codigo):
        d = aplicar_compuerta(
            puntaje_riesgo=0,
            hallazgos=[h(codigo, categoria="duplicado_fraude")],
            monto_total=100.0,
        )
        assert d["regla_aplicada"] == "R-02"
        assert d["aprobador_sugerido"] == "cumplimiento"

    @pytest.mark.parametrize("codigo", sorted(CODIGOS_RECHAZO))
    def test_rechazo_nunca_autoriza_monto(self, codigo):
        d = aplicar_compuerta(
            puntaje_riesgo=0,
            hallazgos=[h(codigo, categoria="duplicado_fraude")],
            monto_total=5000.0,
        )
        assert d["monto_autorizado"] == 0.0

    def test_investigacion_y_rechazo_no_se_solapan(self):
        """Un codigo no puede exigir investigacion y justificar rechazo a la vez:
        o queda duda razonable o no queda."""
        assert not (CODIGOS_INVESTIGACION & CODIGOS_RECHAZO)

    def test_toda_retencion_o_rechazo_exige_confirmacion_humana(self):
        for codigo in sorted(CODIGOS_BLOQUEANTES):
            d = aplicar_compuerta(
                puntaje_riesgo=0, hallazgos=[h(codigo)], monto_total=100.0
            )
            assert d["requiere_confirmacion_humana"] is True, codigo


class TestTrazabilidad:
    @pytest.mark.parametrize(
        "puntaje,hallazgos,monto,no_eval",
        [
            (0, [], 100.0, None),
            (0, [], 80000.0, None),
            (30, [h("TRI-IGV-001")], 1000.0, None),
            (70, [h("TRI-IGV-001")], 1000.0, None),
            (0, [h("TRV-OC-001")], 1000.0, None),
            (0, [h("FRA-DUP-001", categoria="duplicado_fraude")], 1000.0, None),
            (0, [h("FRA-CTA-004", categoria="duplicado_fraude")], 1000.0, None),
            (0, [], 100.0, ["tributario"]),
        ],
    )
    def test_toda_decision_es_reproducible(self, puntaje, hallazgos, monto, no_eval):
        """Cada decision tiene que decir QUE regla la produjo y contra QUE
        umbral se comparo. Un 'R-03' sin el umbral no es reproducible si la
        config cambio."""
        d = aplicar_compuerta(puntaje, hallazgos, monto, no_eval)
        assert d["regla_aplicada"] in REGLAS
        assert isinstance(d["umbral_usado"], float)
        assert d["motivo"]
        DecisionAP.model_validate(d)

    def test_todas_las_reglas_estan_documentadas(self):
        assert set(REGLAS) == {f"R-0{i}" for i in range(1, 9)}
        for descripcion in REGLAS.values():
            assert len(descripcion) > 20


class TestScoring:
    def test_el_puntaje_se_trunca_en_100(self):
        from ap_ops.herramientas import puntaje_determinista

        muchos = [h("FRA-DUP-001") for _ in range(10)]
        assert puntaje_determinista(muchos) == 100

    def test_no_saber_suma_riesgo(self):
        from ap_ops.herramientas import puntaje_determinista

        assert puntaje_determinista([], ["tributario"]) == config.PESO_NO_EVALUABLE
        assert puntaje_determinista([], ["a", "b"]) == 2 * config.PESO_NO_EVALUABLE

    def test_deduplica_por_codigo_conservando_la_mayor_severidad(self):
        from ap_ops.herramientas import deduplicar

        a = h("TRI-IGV-001", severidad=Severidad.BAJA.value, monto_impactado=10.0)
        b = h("TRI-IGV-001", severidad=Severidad.ALTA.value, monto_impactado=50.0)
        unicos = deduplicar([a, b])
        assert len(unicos) == 1
        assert unicos[0]["severidad"] == Severidad.ALTA.value
        assert unicos[0]["monto_impactado"] == 50.0

    def test_el_orden_de_los_hallazgos_es_estable(self):
        """El memo y los tests dorados dependen de un orden reproducible."""
        from ap_ops.herramientas import deduplicar

        entrada = [h("TRI-DET-006"), h("FRA-DUP-001"), h("TRI-IGV-001")]
        una = [x["codigo"] for x in deduplicar(entrada)]
        otra = [x["codigo"] for x in deduplicar(list(reversed(entrada)))]
        assert una == otra
        assert una[0] == "FRA-DUP-001"  # critica primero

    def test_hallazgo_desde_desviacion_marca_la_fuente(self):
        desv = {
            "codigo": "TRI-IGV-001",
            "campo_afectado": "igv",
            "evidencia": "calculo",
            "monto_impactado": 336.0,
        }
        hl = hallazgo_desde_desviacion(desv)
        assert hl["fuente"] == "herramienta"
        assert hl["confianza"] == 1.0
        assert hl["severidad"] == severidad_base("TRI-IGV-001").value
