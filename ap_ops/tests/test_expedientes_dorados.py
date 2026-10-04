"""Regresion de expedientes dorados: el camino determinista completo.

Es la red de seguridad mas valiosa del proyecto y la mas barata: corre el
pipeline entero menos los agentes, sin gastar un token. Si algo se rompe aqui,
se rompe antes de llegar a Fase 2.

Lo que se afirma es el CONJUNTO de codigos, la decision y la regla aplicada.
Nunca el texto libre: el texto puede mejorarse sin que el comportamiento cambie.
"""

from __future__ import annotations

import json

import pytest

from ap_ops import config
from ap_ops.esquemas import ConsolidadoValidacion, DecisionAP
from ap_ops.herramientas import listar_facturas, render_memo_markdown
from ap_ops.scripts.smoke_esquemas import procesar

#: El contrato completo del camino determinista.
#: (codigos esperados, resultado, regla, requiere confirmacion humana)
DORADOS: dict[str, tuple[set[str], str, str, bool]] = {
    "factura_conforme": (set(), "touchless_approve", "R-08", False),
    "factura_igv_erroneo": (
        {"TRI-IGV-001", "TRI-DET-006"},
        "aprobar_con_condiciones",
        "R-06",
        True,
    ),
    "factura_sin_oc": ({"TRV-OC-001"}, "retener", "R-04", True),
    "factura_duplicada": ({"FRA-DUP-001"}, "rechazar", "R-03", True),
    "factura_fraude_cuenta": (
        {"FRA-CTA-004", "FRA-NUE-006", "FRA-UMB-007"},
        "retener",
        "R-02",
        True,
    ),
    "factura_contrato_excedido": (
        {"PRV-LIM-004", "PRV-CND-003"},
        "retener",
        "R-04",
        True,
    ),
    "factura_ocr_sucio": ({"INT-OCR-001"}, "retener", "R-01", True),
}


def test_los_dorados_cubren_todos_los_fixtures():
    """Si alguien agrega un fixture y no su expediente dorado, este test lo
    detecta. Un fixture sin test dorado es un fixture que no prueba nada."""
    assert set(DORADOS) == set(listar_facturas())


@pytest.mark.parametrize("fixture", sorted(DORADOS))
class TestExpedientesDorados:
    def test_codigos_decision_y_regla(self, fixture):
        codigos_esp, resultado_esp, regla_esp, confirma_esp = DORADOS[fixture]
        e = procesar(fixture)
        assert set(e["consolidado"]["codigos"]) == codigos_esp
        assert e["decision"]["resultado"] == resultado_esp
        assert e["decision"]["regla_aplicada"] == regla_esp
        assert e["decision"]["requiere_confirmacion_humana"] is confirma_esp

    def test_los_esquemas_validan(self, fixture):
        e = procesar(fixture)
        cons = {
            k: v
            for k, v in e["consolidado"].items()
            if k not in ("ruta", "codigos", "codigos_bloqueantes")
        }
        ConsolidadoValidacion.model_validate(cons)
        DecisionAP.model_validate(e["decision"])

    def test_el_memo_se_renderiza_y_cita_la_regla(self, fixture):
        e = procesar(fixture)
        memo = render_memo_markdown(e)
        assert e["decision"]["regla_aplicada"] in memo
        assert e["factura"]["id_factura"] in memo
        for codigo in e["consolidado"]["codigos"]:
            assert codigo in memo, f"{codigo} no aparece en el memo"

    def test_el_memo_no_filtra_numeros_de_cuenta(self, fixture):
        """El memo es un artifact que se guarda y se comparte."""
        e = procesar(fixture)
        memo = render_memo_markdown(e)
        cci = (e["factura"].get("cuenta_bancaria") or {}).get("cci")
        if cci:
            assert cci not in memo

    def test_cero_llamadas_al_modelo(self, fixture):
        e = procesar(fixture)
        assert e["llamadas_llm"] == 0
        assert e["tokens_consumidos"] == 0


@pytest.fixture(scope="module")
def expedientes() -> list[dict]:
    """Los 7 expedientes, procesados una sola vez para todo el modulo."""
    return [procesar(n) for n in listar_facturas()]


class TestInvariantesGlobales:
    def test_ninguna_aprobada_con_hallazgo_bloqueante(self, expedientes):
        for e in expedientes:
            if e["consolidado"]["codigos_bloqueantes"]:
                assert e["decision"]["resultado"] in ("retener", "rechazar"), e["nombre_fixture"]

    def test_ninguna_touchless_con_hallazgos(self, expedientes):
        for e in expedientes:
            if e["decision"]["resultado"] == "touchless_approve":
                assert not e["consolidado"]["hallazgos"], e["nombre_fixture"]
                assert not e["consolidado"]["validadores_no_evaluables"], e["nombre_fixture"]

    def test_ninguna_touchless_sobre_el_umbral_de_monto(self, expedientes):
        for e in expedientes:
            if e["decision"]["resultado"] == "touchless_approve":
                assert e["factura"]["total"] <= config.UMBRAL_MONTO_HITL, e["nombre_fixture"]

    def test_la_ruta_del_grafo_es_coherente_con_los_hallazgos(self, expedientes):
        """`nodo_consolidar` enruta por esta clave. Si 'sin_hallazgos' saliera
        con hallazgos presentes, el grafo saltearia el scoring y el plan de
        accion de una factura que los necesita."""
        for e in expedientes:
            c = e["consolidado"]
            esperada = "sin_hallazgos" if not c["hallazgos"] and not c["validadores_no_evaluables"] else "con_hallazgos"
            assert c["ruta"] == esperada, e["nombre_fixture"]

    def test_todo_hallazgo_trae_evidencia_de_herramienta(self, expedientes):
        """En el camino determinista, el 100% de los hallazgos tiene respaldo
        calculado. En Fase 2 el modelo puede agregar fuente='modelo', pero esos
        nunca deben bloquear un pago por si solos."""
        for e in expedientes:
            for h in e["consolidado"]["hallazgos"]:
                assert h["fuente"] == "herramienta", (e["nombre_fixture"], h["codigo"])
                assert h["evidencia"], (e["nombre_fixture"], h["codigo"])
                assert h["confianza"] == 1.0

    def test_el_expediente_es_serializable(self, expedientes):
        """Tiene que poder viajar como estado de sesion y como artifact."""
        for e in expedientes:
            json.dumps(e)

    def test_los_resultados_son_deterministas(self):
        """Dos corridas de la misma factura deben dar exactamente lo mismo.
        Es la propiedad que hace auditable al sistema."""
        for nombre in listar_facturas():
            a, b = procesar(nombre), procesar(nombre)
            for clave in ("consolidado", "decision"):
                assert json.dumps(a[clave], sort_keys=True) == json.dumps(
                    b[clave], sort_keys=True
                ), nombre
