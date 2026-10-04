"""Manejo de fechas. Privado al paquete `herramientas`.

La fecha "hoy" es inyectable por variable de entorno para que los tests de
expedientes dorados sean reproducibles. Sin eso, `factura_fraude_cuenta` dejaria
de ser "proveedor nuevo" cuando pasen 90 dias y los tests empezarian a fallar
solos, lo que es la peor clase de test: el que falla sin que nadie haya tocado
nada.

No se expone como parametro de las herramientas a proposito: el LLM no deberia
poder elegir la fecha de referencia del analisis.
"""

from __future__ import annotations

import os
from datetime import date, datetime

VAR_ENTORNO = "AP_OPS_FECHA_REFERENCIA"


def hoy() -> date:
    """Fecha de referencia del analisis. Hoy, o lo que indique el entorno."""
    override = os.environ.get(VAR_ENTORNO)
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError:
            pass
    return date.today()


def parsear(iso: str | None) -> date | None:
    """ISO 8601 -> date, o None si no se puede parsear.

    Devuelve None en lugar de levantar: una fecha ilegible del OCR es un dato
    faltante que lleva a 'no_evaluable', no una excepcion que tumbe el pipeline.
    """
    if not iso:
        return None
    try:
        return date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return None


def dias_desde(iso: str | None) -> int | None:
    """Dias transcurridos desde la fecha dada. Negativo si esta en el futuro."""
    d = parsear(iso)
    if d is None:
        return None
    return (hoy() - d).days


def ahora_iso() -> str:
    """Timestamp ISO 8601 con zona, para `ExpedienteAP.generado_en`."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
