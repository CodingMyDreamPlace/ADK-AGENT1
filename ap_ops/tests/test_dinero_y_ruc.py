"""Tests de la aritmetica de dinero y del digito verificador del RUC.

Son los tests mas importantes del proyecto aunque parezcan los mas triviales:
cubren exactamente las dos cosas que un LLM hace mal y que un auditor va a
re-verificar con una calculadora.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ap_ops.herramientas._dinero import (
    dec,
    difiere,
    enmascarar_cuenta,
    flt,
    pct_diferencia,
    redondear,
)
from ap_ops.herramientas._ruc import completar_ruc, digito_verificador, validar_ruc


class TestDinero:
    def test_el_caso_que_justifica_todo_el_modulo(self):
        """Con float, 10233.33 * 0.18 da 1841.9994 y el validador reporta un
        hallazgo que no existe. Con Decimal da exactamente 1842.00."""
        assert 10233.33 * 0.18 != 1842.00  # el bug que estamos evitando
        assert flt(redondear(dec(10233.33) * dec(0.18))) == 1842.00

    def test_round_half_up_no_bancario(self):
        """Python redondea 2.5 -> 2 (half-even). Una caja registradora no."""
        assert round(2.5) == 2  # comportamiento de Python
        assert flt(redondear(dec("2.125"))) == 2.13
        assert flt(redondear(dec("2.135"))) == 2.14

    def test_dec_no_arrastra_el_error_del_float(self):
        assert dec(0.1) == Decimal("0.1")
        assert dec(0.1) != Decimal(0.1)

    def test_dec_tolera_basura(self):
        assert dec("no es un numero") == Decimal("0")
        assert dec(None) == Decimal("0")

    @pytest.mark.parametrize(
        "a,b,tol,esperado",
        [
            (1842.00, 1842.00, 0.05, False),
            (1842.00, 1842.04, 0.05, False),  # dentro de tolerancia
            (1842.00, 1842.06, 0.05, True),  # fuera
            (1500.00, 1842.00, 0.05, True),
        ],
    )
    def test_difiere_respeta_la_tolerancia(self, a, b, tol, esperado):
        assert difiere(dec(a), dec(b), tol) is esperado

    def test_pct_diferencia_no_divide_por_cero(self):
        """Una OC con monto 0 es un dato malo, no una excepcion que tumbe el pipeline."""
        assert pct_diferencia(dec(100), dec(0)) == Decimal("0")

    def test_pct_diferencia_con_signo(self):
        assert pct_diferencia(dec(110), dec(100)) == Decimal("10.00")
        assert pct_diferencia(dec(90), dec(100)) == Decimal("-10.00")

    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("00219300123456789012", "****9012"),
            ("193-1234567-0-55", "****7055"),
            (None, "(sin dato)"),
            ("", "(sin dato)"),
            ("12", "**"),
        ],
    )
    def test_enmascarar_cuenta(self, entrada, esperado):
        assert enmascarar_cuenta(entrada) == esperado

    def test_enmascarar_nunca_filtra_el_numero_completo(self):
        completo = "00219300123456789012"
        mask = enmascarar_cuenta(completo)
        assert completo not in mask
        assert len(mask) == 8


class TestRuc:
    @pytest.mark.parametrize(
        "ruc,valido,tipo",
        [
            ("20512345671", True, "20"),
            ("20478965410", True, "20"),
            ("20600123450", True, "20"),
            ("20100047218", True, "20"),
            ("10456789124", True, "10"),
            ("20512345672", False, "20"),  # digito verificador alterado
            ("30123456789", False, "desconocido"),  # prefijo inexistente
            ("2051234567", False, "desconocido"),  # 10 digitos
            ("", False, "desconocido"),
        ],
    )
    def test_validar_ruc(self, ruc, valido, tipo):
        assert validar_ruc(ruc) == (valido, tipo)

    def test_completar_ruc_produce_rucs_validos(self):
        for base in ["2051234567", "1045678912", "2060012345", "2010004721"]:
            ruc = completar_ruc(base)
            assert len(ruc) == 11
            assert validar_ruc(ruc)[0] is True

    def test_digito_verificador_casos_borde(self):
        """resto 10 -> 0 y resto 11 -> 1 son los dos casos que se suelen
        implementar mal."""
        d = digito_verificador("2051234567")
        assert 0 <= d <= 9

    def test_un_solo_digito_cambiado_invalida(self):
        """Es la propiedad que hace util al digito verificador: detecta el
        error de tipeo de un digito."""
        ruc = completar_ruc("2051234567")
        for i in range(10):
            alterado = list(ruc)
            alterado[i] = str((int(alterado[i]) + 1) % 10)
            assert validar_ruc("".join(alterado))[0] is False, f"posicion {i}"
