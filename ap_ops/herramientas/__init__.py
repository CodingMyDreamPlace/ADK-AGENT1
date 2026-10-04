"""Herramientas deterministas de AP Ops. CERO LLM.

Todo lo que vive en este paquete tiene una sola respuesta correcta y la calcula
Python. Es la mitad determinista del sistema, y la frontera no es arbitraria:

    LO QUE CALCULA PYTHON (aqui)          LO QUE JUZGA EL MODELO (nodos/)
    ------------------------------        -------------------------------
    IGV, aritmetica, sumas                severidad proporcional al monto
    digito verificador del RUC            titulo legible del hallazgo
    diferencias de cantidad y precio       si bloquea el pago
    huella de duplicado                   correlacion entre validadores
    comparacion exacta de CCI             narrativa ejecutiva
    saldo de contrato                     plan de accion
    la decision de pago (R-01..R-08)      conversacion y explicacion

La regla: si tiene una respuesta correcta unica, es Python. Si requiere juicio,
es el modelo. La aritmetica es el modo de falla numero uno de un LLM en Cuentas
por Pagar, y es exactamente lo que un auditor va a re-verificar.

Las cuatro `analizar_*` son las que los validadores reciben como `tools`. Las
otras las usan los FunctionNode del grafo, que no tienen modelo asociado.
"""

from __future__ import annotations

from .conciliacion import analizar_tres_vias
from .decision import REGLAS, aplicar_compuerta
from .duplicados import analizar_duplicados_fraude, huella
from .memo import catalogo_markdown, render_memo_markdown, resumen_una_linea
from .proveedor import analizar_proveedor_contrato
from .repositorio import (
    DatoNoDisponible,
    buscar_historial,
    buscar_nr,
    buscar_oc,
    buscar_proveedor,
    cargar_factura,
    limpiar_cache,
    listar_facturas,
)
from .scoring import (
    consolidar,
    deduplicar,
    hallazgo_desde_desviacion,
    puntaje_determinista,
)
from .tributario import analizar_tributario

__all__ = [
    # --- las 4 que son `tools` de los validadores -----------------------
    "analizar_tres_vias",
    "analizar_tributario",
    "analizar_duplicados_fraude",
    "analizar_proveedor_contrato",
    # --- acceso a datos (el seam a BigQuery / ERP) ----------------------
    "cargar_factura",
    "listar_facturas",
    "buscar_oc",
    "buscar_nr",
    "buscar_proveedor",
    "buscar_historial",
    "limpiar_cache",
    "DatoNoDisponible",
    # --- consolidacion y puntaje ----------------------------------------
    "consolidar",
    "deduplicar",
    "puntaje_determinista",
    "hallazgo_desde_desviacion",
    # --- la compuerta de decision ---------------------------------------
    "aplicar_compuerta",
    "REGLAS",
    # --- utilidades -----------------------------------------------------
    "huella",
    "render_memo_markdown",
    "resumen_una_linea",
    "catalogo_markdown",
]
