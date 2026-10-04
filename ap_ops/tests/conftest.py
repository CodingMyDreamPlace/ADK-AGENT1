"""Configuracion compartida de los tests.

La fecha de referencia se fija ANTES de cualquier import de `ap_ops`, porque
`_fechas.hoy()` lee la variable de entorno en cada llamada pero varios fixtures
dependen de ella para seguir significando lo mismo:

  * `factura_fraude_cuenta` solo es "proveedor nuevo" mientras la alta
    (2026-09-20) este dentro de los 90 dias.
  * ninguna factura debe caer fuera del periodo deducible de 365 dias.

Sin esta fijacion, los tests empezarian a fallar solos con el paso del tiempo,
que es la peor clase de test: el que se rompe sin que nadie haya tocado nada.
"""

from __future__ import annotations

import os

os.environ["AP_OPS_FECHA_REFERENCIA"] = "2026-10-03"

import pytest  # noqa: E402

from ap_ops.herramientas import repositorio  # noqa: E402


@pytest.fixture(autouse=True)
def _cache_limpio():
    """Invalida el cache de fixtures entre tests.

    `repositorio` cachea los maestros con `lru_cache` porque son de solo
    lectura y el pipeline los consulta varias veces por factura. Los tests que
    manipulan archivos en disco necesitan que ese cache no sobreviva.
    """
    repositorio.limpiar_cache()
    yield
    repositorio.limpiar_cache()
