"""Tests de las 4 herramientas de analisis.

Cada fixture fue calibrado para disparar un validador especifico. Estos tests
afirman sobre el CONJUNTO de codigos producidos, no sobre el texto: el conjunto
es estable, el texto puede mejorarse sin romper nada.
"""

from __future__ import annotations

import pytest

from ap_ops.herramientas import (
    analizar_duplicados_fraude,
    analizar_proveedor_contrato,
    analizar_tres_vias,
    analizar_tributario,
)

ANALIZADORES = {
    "tres_vias": analizar_tres_vias,
    "tributario": analizar_tributario,
    "duplicados_fraude": analizar_duplicados_fraude,
    "proveedor_contrato": analizar_proveedor_contrato,
}


def codigos(resultado: dict) -> set[str]:
    return {d["codigo"] for d in resultado.get("desviaciones", [])}


def todos_los_codigos(fixture: str) -> set[str]:
    s: set[str] = set()
    for fn in ANALIZADORES.values():
        s |= codigos(fn(fixture))
    return s


# --------------------------------------------------------------- aislamiento

#: El contrato de diseno de los fixtures: cada uno dispara exactamente estos
#: codigos. Si un cambio en una herramienta hace aparecer o desaparecer un
#: codigo, este test lo detecta.
ESPERADOS: dict[str, set[str]] = {
    "factura_conforme": set(),
    "factura_igv_erroneo": {"TRI-IGV-001", "TRI-DET-006"},
    "factura_sin_oc": {"TRV-OC-001"},
    "factura_duplicada": {"FRA-DUP-001"},
    "factura_fraude_cuenta": {"FRA-CTA-004", "FRA-NUE-006", "FRA-UMB-007"},
    "factura_contrato_excedido": {"PRV-LIM-004", "PRV-CND-003"},
    "factura_ocr_sucio": {"INT-OCR-001"},
}


@pytest.mark.parametrize("fixture,esperados", sorted(ESPERADOS.items()))
def test_cada_fixture_dispara_exactamente_sus_codigos(fixture, esperados):
    assert todos_los_codigos(fixture) == esperados


# --------------------------------------------------------------- tributario


class TestTributario:
    def test_igv_correcto_no_es_hallazgo(self):
        r = analizar_tributario("factura_conforme")
        assert r["igv_correcto"] is True
        assert r["igv_declarado"] == 720.00
        assert r["igv_calculado"] == 720.00
        assert "TRI-IGV-001" not in codigos(r)

    def test_igv_erroneo_reporta_la_diferencia_exacta(self):
        r = analizar_tributario("factura_igv_erroneo")
        assert r["igv_declarado"] == 1500.00
        assert r["igv_calculado"] == 1836.00
        assert r["diferencia_igv"] == -336.00
        assert r["igv_correcto"] is False

    def test_la_evidencia_contiene_el_calculo_literal(self):
        """El campo `evidencia` es lo que un auditor relee. Tiene que traer los
        numeros, no una parafrasis."""
        r = analizar_tributario("factura_igv_erroneo")
        ev = next(d["evidencia"] for d in r["desviaciones"] if d["codigo"] == "TRI-IGV-001")
        assert "10,200.00" in ev
        assert "1,836.00" in ev
        assert "1,500.00" in ev
        assert "-336.00" in ev

    def test_aritmetica_cuadra_en_todas_las_facturas_legibles(self):
        for fixture in ESPERADOS:
            if fixture == "factura_ocr_sucio":
                continue  # confianza insuficiente: no se evalua
            r = analizar_tributario(fixture)
            assert r["aritmetica_correcta"] is True, fixture
            assert r["lineas_cuadran"] is True, fixture

    def test_detraccion_requerida_sin_cuenta_de_detracciones(self):
        r = analizar_tributario("factura_igv_erroneo")
        assert r["detraccion_requerida"] is True
        assert r["codigo_detraccion"] == "037"
        assert r["porcentaje_detraccion"] == 0.12
        assert r["monto_detraccion"] == 1404.00
        assert r["cuenta_detraccion_presente"] is False
        assert "TRI-DET-006" in codigos(r)

    def test_compuerta_de_confianza_suprime_todo_calculo(self):
        """Validar cifras mal leidas produce hallazgos falsos. La herramienta
        se niega a evaluar y lo dice."""
        r = analizar_tributario("factura_ocr_sucio")
        assert r["confianza_insuficiente"] is True
        assert r["confianza_ocr"] == 0.42
        assert codigos(r) == {"INT-OCR-001"}
        # El RUC de esa factura ES invalido, pero NO se reporta: no se valida
        # lo que no se pudo leer.
        assert "TRI-RUC-003" not in codigos(r)


# --------------------------------------------------------------- tres vias


class TestTresVias:
    def test_conciliacion_completa(self):
        r = analizar_tres_vias("factura_conforme")
        assert r["orden_compra_encontrada"] is True
        assert r["nota_recepcion_encontrada"] is True
        assert r["lineas_conciliadas"] == 1
        assert r["lineas_con_diferencia"] == 0
        assert r["dentro_tolerancia"] is True

    def test_sin_oc_no_hay_conciliacion_posible(self):
        r = analizar_tres_vias("factura_sin_oc")
        assert r["orden_compra_encontrada"] is False
        assert r["dentro_tolerancia"] is False
        assert codigos(r) == {"TRV-OC-001"}

    def test_sin_oc_mantiene_la_forma_del_resultado(self):
        """El esquema ResultadoTresVias exige todos los campos. Un dict con
        claves faltantes haria que el modelo las invente."""
        completo = analizar_tres_vias("factura_conforme")
        parcial = analizar_tres_vias("factura_sin_oc")
        assert set(completo) == set(parcial)

    def test_reporta_las_tolerancias_que_aplico(self):
        """Un 'dentro_tolerancia=True' sin decir cual fue la tolerancia no es
        defendible ante un auditor."""
        r = analizar_tres_vias("factura_conforme")
        assert r["tolerancia_cantidad_pct"] == 2.0
        assert r["tolerancia_precio_pct"] == 2.0
        assert r["tolerancia_monto_pct"] == 2.0


# --------------------------------------------------------------- fraude


class TestDuplicadosFraude:
    def test_duplicado_exacto(self):
        r = analizar_duplicados_fraude("factura_duplicada")
        assert "FRA-DUP-001" in codigos(r)
        exacta = [
            c for c in r["duplicados_detectados"] if c["tipo_coincidencia"] == "exacta"
        ]
        assert len(exacta) == 1
        assert exacta[0]["estado_pago_previa"] == "pagada"

    def test_cuenta_que_no_coincide_es_la_senal_principal(self):
        r = analizar_duplicados_fraude("factura_fraude_cuenta")
        assert r["cuenta_bancaria_coincide"] is False
        assert "FRA-CTA-004" in codigos(r)

    def test_las_cuentas_salen_siempre_enmascaradas(self):
        """Este resultado viaja a prompts, estado de sesion, base de datos y
        trazas de OTel. Nunca debe llevar el numero completo."""
        import json

        r = analizar_duplicados_fraude("factura_fraude_cuenta")
        texto = json.dumps(r)
        assert "01801800778100991234" not in texto  # CCI declarado
        assert "01145600111222333444" not in texto  # CCI registrado
        assert r["cuenta_declarada_enmascarada"].startswith("****")
        assert r["cuenta_registrada_enmascarada"].startswith("****")

    def test_las_tres_patas_del_patron_de_fraude(self):
        """Por separado son senales debiles. Juntas son un fraude en curso, y
        correlacionarlas es trabajo del agente de scoring, no de la herramienta."""
        r = analizar_duplicados_fraude("factura_fraude_cuenta")
        assert r["cuenta_bancaria_coincide"] is False
        assert r["proveedor_nuevo"] is True
        assert r["dias_desde_alta_proveedor"] == 13
        assert r["monto_bajo_umbral_sospechoso"] is True
        assert r["distancia_al_umbral"] == 50.00
        assert len(r["indicadores_fraude"]) == 3

    def test_coincidencia_debil_se_reporta_como_dato_no_como_hallazgo(self):
        """factura_conforme comparte monto con dos facturas historicas del mismo
        proveedor (un abono mensual fijo). Es dato, no desviacion."""
        r = analizar_duplicados_fraude("factura_conforme")
        debiles = [
            c
            for c in r["duplicados_detectados"]
            if c["tipo_coincidencia"] == "monto_proveedor"
        ]
        assert len(debiles) == 2
        assert codigos(r) == set()

    def test_huella_estable_e_insensible_al_redondeo(self):
        from ap_ops.herramientas import huella

        a = huella("20512345671", "F001", "00234", 4720.0, "2026-09-26")
        b = huella("20512345671", "F001", "00234", 4720.004, "2026-09-26")
        c = huella("20512345671", "F001", "00234", 4720.5, "2026-09-26")
        assert a == b
        assert a != c


# --------------------------------------------------------------- proveedor


class TestProveedorContrato:
    def test_proveedor_conforme(self):
        r = analizar_proveedor_contrato("factura_conforme")
        assert r["proveedor_en_maestro"] is True
        assert r["proveedor_habilitado"] is True
        assert r["condicion_pago_coincide"] is True
        assert r["excede_contrato"] is False
        assert codigos(r) == set()

    def test_excede_contrato_con_el_calculo_exacto(self):
        r = analizar_proveedor_contrato("factura_contrato_excedido")
        assert r["limite_contrato"] == 300000.0
        assert r["monto_consumido"] == 295000.0
        assert r["monto_disponible"] == 5000.0
        assert r["excede_contrato"] is True
        assert r["exceso_contrato"] == 9160.0
        assert "PRV-LIM-004" in codigos(r)

    def test_condicion_de_pago_distinta_es_un_hallazgo_financiero(self):
        r = analizar_proveedor_contrato("factura_contrato_excedido")
        assert r["condicion_pago_declarada"] == "contado"
        assert r["condicion_pago_contractual"] == "credito_45"
        assert r["condicion_pago_coincide"] is False
        assert "PRV-CND-003" in codigos(r)

    def test_proveedor_nuevo_esta_habilitado(self):
        """'nuevo' no es 'suspendido': un proveedor recien dado de alta puede
        operar. El riesgo lo capta el validador de fraude, no este."""
        r = analizar_proveedor_contrato("factura_fraude_cuenta")
        assert r["estado_proveedor"] == "nuevo"
        assert r["proveedor_habilitado"] is True
        assert codigos(r) == set()
