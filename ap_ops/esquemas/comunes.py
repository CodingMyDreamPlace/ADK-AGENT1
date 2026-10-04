"""Tipos compartidos por todos los esquemas de AP Ops.

REGLA DE DISENO QUE ATRAVIESA TODO ESTE PAQUETE
-----------------------------------------------
Cualquier modelo que termine siendo `output_schema` de un LlmAgent tiene que ser
traducible a un JSON Schema que Gemini acepte. Eso prohibe cuatro cosas:

  * `Decimal`          -> no tiene representacion en JSON Schema. Usamos `float`.
  * `date` / `datetime`-> idem. Usamos `str` en formato ISO 8601.
  * `dict[str, Any]`   -> produce un objeto sin propiedades declaradas, que el
                          modelo rellena con lo que se le ocurra. Usamos
                          `DatoVerificado` en su lugar.
  * recursion          -> genera `$ref` circular. Ningun modelo se referencia
                          a si mismo, ni directa ni indirectamente.

`Decimal` SI se usa, pero encerrado dentro de `herramientas/`, donde se hace la
aritmetica con ROUND_HALF_UP a 2 decimales. Se convierte a `float` recien al
cruzar la frontera hacia el LLM. Si dejaramos floats en el calculo, el IGV
daria 1899.9999999998 y el validador reportaria hallazgos inexistentes.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Severidad(str, Enum):
    """Severidad de un hallazgo.

    Hereda de `str` para que serialice como `"alta"` y no como `Severidad.ALTA`,
    y para que el JSON Schema salga como un enum de strings legible por el modelo.
    """

    INFORMATIVA = "informativa"
    BAJA = "baja"
    MEDIA = "media"
    ALTA = "alta"
    CRITICA = "critica"


#: Orden total para poder calcular el maximo. Un `str` Enum no es comparable por
#: defecto, y no queremos usar IntEnum porque serializaria como numero.
ORDEN_SEVERIDAD: dict[Severidad, int] = {
    Severidad.INFORMATIVA: 0,
    Severidad.BAJA: 1,
    Severidad.MEDIA: 2,
    Severidad.ALTA: 3,
    Severidad.CRITICA: 4,
}


def severidad_maxima(severidades: list[Severidad]) -> Severidad:
    """Devuelve la severidad mas alta de la lista, o INFORMATIVA si esta vacia."""
    if not severidades:
        return Severidad.INFORMATIVA
    return max(severidades, key=lambda s: ORDEN_SEVERIDAD[s])


Moneda = Literal["PEN", "USD", "EUR"]

#: Codigos SUNAT de comprobante. Solo la factura (01) da derecho a credito
#: fiscal de IGV: una boleta (03) en el flujo de AP es por si misma un hallazgo,
#: y por eso esta en el Literal aunque "no deberia" llegar nunca.
TipoComprobante = Literal["01", "03", "07", "08"]

NOMBRE_COMPROBANTE: dict[str, str] = {
    "01": "Factura",
    "03": "Boleta de venta",
    "07": "Nota de credito",
    "08": "Nota de debito",
}

#: A que validador pertenece un hallazgo. Sirve para agrupar en el memo y para
#: etiquetar las metricas de observabilidad (`ap_ops.hallazgos{categoria}`).
Categoria = Literal[
    "tres_vias",
    "tributario",
    "duplicado_fraude",
    "proveedor_contrato",
    "integridad_datos",
]


class DatoVerificado(BaseModel):
    """Par nombre/valor con procedencia explicita.

    Existe para reemplazar `dict[str, Any]`, que no se puede usar en un
    `output_schema`. El campo `fuente` es lo que permite auditar despues si una
    cifra la calculo una herramienta determinista o la escribio el modelo.
    """

    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(description="Que se verifico, p.ej. 'igv_calculado'.")
    valor: str = Field(description="El valor, siempre como string.")
    fuente: Literal["herramienta", "modelo"] = Field(
        default="herramienta",
        description="Quien produjo el dato. 'herramienta' es confiable; 'modelo' no.",
    )
