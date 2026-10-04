"""Aritmetica de dinero. Privado al paquete `herramientas`.

Por que existe este modulo en lugar de usar floats directamente:

    >>> 10233.33 * 0.18
    1841.9994

El IGV correcto es 1842.00. Con floats, el validador tributario reportaria una
diferencia de S/ 0.0006 como hallazgo, en TODAS las facturas. Con `Decimal` y
ROUND_HALF_UP da exactamente 1842.00.

La regla del paquete: `Decimal` entra y sale solo de aqui. Las herramientas
calculan con `Decimal` y devuelven `float` al cruzar hacia los esquemas, porque
`Decimal` no tiene representacion en el JSON Schema que consume Gemini.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTIMO = Decimal("0.01")


def dec(valor: float | int | str | Decimal) -> Decimal:
    """Convierte a Decimal pasando por str.

    `Decimal(0.1)` da 0.1000000000000000055511151231257827021181583404541015625.
    `Decimal("0.1")` da 0.1. De ahi el `str()` intermedio: es la unica forma de
    no arrastrar el error del float de entrada.
    """
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def redondear(valor: Decimal) -> Decimal:
    """Redondea a 2 decimales con ROUND_HALF_UP.

    ROUND_HALF_UP y no el default ROUND_HALF_EVEN de Python: el redondeo
    bancario ("half to even") hace que 2.5 -> 2 y 3.5 -> 4, que no es lo que
    hace una caja registradora ni lo que espera un contador peruano.
    """
    return valor.quantize(CENTIMO, rounding=ROUND_HALF_UP)


def flt(valor: Decimal) -> float:
    """Decimal -> float, ya redondeado. Usar SOLO al devolver desde una herramienta."""
    return float(redondear(valor))


def difiere(a: Decimal, b: Decimal, tolerancia: float) -> bool:
    """True si |a - b| supera la tolerancia.

    Nunca comparar montos con `==`. Esta funcion es el unico comparador de
    dinero del paquete.
    """
    return abs(a - b) > dec(tolerancia)


def pct_diferencia(valor: Decimal, referencia: Decimal) -> Decimal:
    """Diferencia porcentual de `valor` respecto de `referencia`.

    Devuelve 0 si la referencia es 0, en lugar de explotar: una OC con monto 0
    es un dato malo, no una division por cero que deba tumbar el pipeline.
    """
    if referencia == 0:
        return Decimal("0")
    return redondear((valor - referencia) / referencia * dec(100))


def enmascarar_cuenta(numero: str | None) -> str:
    """Deja solo los ultimos 4 digitos: '00212300123456789012' -> '****9012'.

    Los resultados de los validadores viajan a prompts de modelos, al estado de
    sesion, a la base de datos y a trazas de OpenTelemetry. Un numero de cuenta
    completo no deberia estar en ninguno de esos cuatro lugares. Para la
    comparacion exacta alcanza un booleano.
    """
    if not numero:
        return "(sin dato)"
    limpio = "".join(c for c in numero if c.isdigit())
    if len(limpio) <= 4:
        return "*" * len(limpio)
    return "****" + limpio[-4:]
