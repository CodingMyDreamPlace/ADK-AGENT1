"""Catalogo cerrado de codigos de hallazgo.

Por que el codigo lo asigna la HERRAMIENTA y no el modelo: el mapeo
"este chequeo fallo" -> "este codigo" es deterministico por definicion. No hay
juicio involucrado. Si lo dejaramos al modelo, inventaria variantes
('TRI-IGV-01', 'IGV-001', 'TRI_IGV_1') y los tests por conjunto de codigos
dejarian de servir.

Lo que el modelo SI aporta sobre cada desviacion: la severidad proporcional al
monto y al riesgo, un titulo legible, la ruta exacta del campo afectado, y el
juicio de si bloquea el pago. Eso no tiene forma cerrada.

Prefijos, uno por validador:
    TRV  conciliacion a tres vias
    TRI  tributario
    FRA  duplicados y fraude
    PRV  proveedor y contrato
    INT  integridad de datos (transversal)
"""

from __future__ import annotations

from ..esquemas import Categoria, Severidad

#: codigo -> (titulo sugerido, categoria)
CATALOGO: dict[str, tuple[str, Categoria]] = {
    # ------------------------------------------------ tres vias
    "TRV-OC-001": ("Factura sin orden de compra referenciada", "tres_vias"),
    "TRV-OC-002": ("La orden de compra referenciada no existe", "tres_vias"),
    "TRV-OC-003": ("Orden de compra cerrada o anulada", "tres_vias"),
    "TRV-OC-004": ("RUC de la factura no coincide con el de la orden de compra", "tres_vias"),
    "TRV-NR-005": ("Sin nota de recepcion: no hay constancia de entrega", "tres_vias"),
    "TRV-NR-006": ("Recepcion parcial o con observaciones", "tres_vias"),
    "TRV-CAN-007": ("Diferencia de cantidad fuera de tolerancia", "tres_vias"),
    "TRV-PRE-008": ("Diferencia de precio unitario fuera de tolerancia", "tres_vias"),
    "TRV-MON-009": ("Monto facturado excede el autorizado en la orden de compra", "tres_vias"),
    "TRV-LIN-010": ("Linea facturada que no existe en la orden de compra", "tres_vias"),
    # ------------------------------------------------ tributario
    "TRI-IGV-001": ("IGV declarado no coincide con el calculado", "tributario"),
    "TRI-ARI-002": ("La aritmetica del comprobante no cuadra", "tributario"),
    "TRI-RUC-003": ("RUC invalido: digito verificador incorrecto", "tributario"),
    "TRI-CMP-004": ("Tipo de comprobante sin derecho a credito fiscal", "tributario"),
    "TRI-PER-005": ("Comprobante fuera del periodo deducible", "tributario"),
    "TRI-DET-006": ("Detraccion requerida sin cuenta de detracciones", "tributario"),
    "TRI-LIN-007": ("La suma de las lineas no coincide con el subtotal", "tributario"),
    "TRI-FUT-008": ("Fecha de emision posterior a la fecha actual", "tributario"),
    # ------------------------------------------------ duplicados y fraude
    "FRA-DUP-001": ("Duplicado exacto: el comprobante ya esta registrado", "duplicado_fraude"),
    "FRA-DUP-002": ("Duplicado por huella: mismos datos clave", "duplicado_fraude"),
    "FRA-DUP-003": ("Coincidencia de monto y fecha con un comprobante previo", "duplicado_fraude"),
    "FRA-CTA-004": (
        "La cuenta de abono no coincide con ninguna registrada del proveedor",
        "duplicado_fraude",
    ),
    "FRA-CTA-005": ("El titular de la cuenta no corresponde a la razon social", "duplicado_fraude"),
    "FRA-NUE-006": ("Proveedor recien dado de alta con monto significativo", "duplicado_fraude"),
    "FRA-UMB-007": (
        "Monto apenas por debajo del umbral de aprobacion: posible fraccionamiento",
        "duplicado_fraude",
    ),
    # ------------------------------------------------ proveedor y contrato
    "PRV-MAE-001": ("Proveedor no registrado en el maestro", "proveedor_contrato"),
    "PRV-EST-002": ("Proveedor suspendido o bloqueado", "proveedor_contrato"),
    "PRV-CND-003": ("Condicion de pago distinta de la contractual", "proveedor_contrato"),
    "PRV-LIM-004": ("El monto excede el limite de contrato disponible", "proveedor_contrato"),
    "PRV-RAZ-005": ("Razon social distinta de la registrada en el maestro", "proveedor_contrato"),
    # ------------------------------------------------ integridad
    "INT-OCR-001": (
        "Confianza de extraccion insuficiente: no se puede validar el documento",
        "integridad_datos",
    ),
}


#: Severidad BASE por codigo. No es la severidad final del Hallazgo.
#:
#: El modelo asigna la severidad definitiva ajustandola al monto y al contexto
#: (una diferencia de IGV de S/ 3 no es lo mismo que una de S/ 30.000). Esta
#: tabla cumple otra funcion: alimenta `puntaje_determinista`, que es el
#: CONTROL CRUZADO contra el puntaje del modelo.
#:
#: Si el modelo dice 15 y la tabla dice 70, eso no es un empate que haya que
#: promediar: es una SenalOps('desacuerdo_validadores') que manda la factura a
#: revision humana. Tener dos estimadores independientes es lo que permite
#: detectar cuando uno de los dos se equivoco.
SEVERIDAD_BASE: dict[str, Severidad] = {
    # tres vias
    "TRV-OC-001": Severidad.ALTA,
    "TRV-OC-002": Severidad.ALTA,
    "TRV-OC-003": Severidad.ALTA,
    "TRV-OC-004": Severidad.CRITICA,
    "TRV-NR-005": Severidad.ALTA,
    "TRV-NR-006": Severidad.MEDIA,
    "TRV-CAN-007": Severidad.MEDIA,
    "TRV-PRE-008": Severidad.ALTA,
    "TRV-MON-009": Severidad.ALTA,
    "TRV-LIN-010": Severidad.ALTA,
    # tributario
    "TRI-IGV-001": Severidad.MEDIA,
    "TRI-ARI-002": Severidad.ALTA,
    "TRI-RUC-003": Severidad.CRITICA,
    "TRI-CMP-004": Severidad.ALTA,
    "TRI-PER-005": Severidad.MEDIA,
    "TRI-DET-006": Severidad.MEDIA,
    "TRI-LIN-007": Severidad.MEDIA,
    "TRI-FUT-008": Severidad.ALTA,
    # duplicados y fraude
    "FRA-DUP-001": Severidad.CRITICA,
    "FRA-DUP-002": Severidad.CRITICA,
    "FRA-DUP-003": Severidad.ALTA,
    "FRA-CTA-004": Severidad.CRITICA,
    "FRA-CTA-005": Severidad.ALTA,
    "FRA-NUE-006": Severidad.MEDIA,
    "FRA-UMB-007": Severidad.MEDIA,
    # proveedor y contrato
    "PRV-MAE-001": Severidad.ALTA,
    "PRV-EST-002": Severidad.CRITICA,
    "PRV-CND-003": Severidad.MEDIA,
    "PRV-LIM-004": Severidad.ALTA,
    "PRV-RAZ-005": Severidad.MEDIA,
    # integridad
    "INT-OCR-001": Severidad.ALTA,
}

#: Codigos que por si solos impiden liberar el pago, sin importar el puntaje.
#: Son los casos donde pagar seria un error material, no una desviacion menor.
CODIGOS_BLOQUEANTES: frozenset[str] = frozenset(
    {
        "FRA-DUP-001",  # doble pago
        "FRA-DUP-002",  # doble pago
        "FRA-CTA-004",  # desvio de pago
        "TRI-RUC-003",  # RUC invalido: el comprobante no es valido
        "TRV-OC-001",  # sin OC: nadie autorizo la compra
        "TRV-OC-002",  # la OC referenciada no existe
        "TRV-OC-004",  # la OC es de otro proveedor
        "PRV-EST-002",  # proveedor suspendido o bloqueado
        "PRV-MAE-001",  # proveedor no registrado
        "PRV-LIM-004",  # excede el contrato: requiere adenda antes de pagar
        "INT-OCR-001",  # no se pudo leer el documento
    }
)

#: Codigos que exigen INVESTIGACION, no rechazo.
#:
#: Esta distincion es la decision de politica mas importante del sistema. Una
#: cuenta de abono que no coincide con la registrada probablemente significa que
#: un tercero intercepto la factura. La factura en si puede ser perfectamente
#: legitima, y el proveedor espera su pago.
#:
#: Rechazarla seria castigar al proveedor por un ataque que sufrio la empresa, y
#: ademas cerraria el caso sin averiguar si hay un patron mas amplio. Lo
#: correcto es RETENER, escalar a cumplimiento y verificar al beneficiario por
#: un canal independiente.
CODIGOS_INVESTIGACION: frozenset[str] = frozenset(
    {
        "FRA-CTA-004",  # cuenta de abono distinta de la registrada
        "FRA-CTA-005",  # titular que no corresponde a la razon social
    }
)

#: Codigos de error material CONFIRMADO: no hay nada que investigar, se rechaza.
#:
#: La diferencia con CODIGOS_INVESTIGACION es si queda una duda razonable. Un
#: duplicado exacto contra un comprobante ya pagado no deja duda: pagarlo otra
#: vez es un doble pago. Un proveedor bloqueado tampoco: la decision de
#: bloquearlo ya la tomo alguien con autoridad para hacerlo.
CODIGOS_RECHAZO: frozenset[str] = frozenset(
    {
        "FRA-DUP-001",  # el comprobante ya esta registrado y pagado
        "FRA-DUP-002",  # misma huella que un comprobante previo
        "TRI-RUC-003",  # RUC invalido: el comprobante no es valido
        "TRV-OC-004",  # la OC pertenece a otro proveedor
        "PRV-EST-002",  # proveedor suspendido o bloqueado
    }
)


def titulo(codigo: str) -> str:
    """Titulo sugerido para un codigo. El modelo puede refinarlo, no inventarlo."""
    return CATALOGO.get(codigo, ("Hallazgo sin catalogar", "integridad_datos"))[0]


def severidad_base(codigo: str) -> Severidad:
    """Severidad de referencia del codigo, para el puntaje determinista."""
    return SEVERIDAD_BASE.get(codigo, Severidad.MEDIA)


def bloquea_pago(codigo: str) -> bool:
    """True si el codigo impide liberar el pago por si solo."""
    return codigo in CODIGOS_BLOQUEANTES


def requiere_investigacion(codigo: str) -> bool:
    """True si el codigo exige retener y escalar, en lugar de rechazar."""
    return codigo in CODIGOS_INVESTIGACION


def justifica_rechazo(codigo: str) -> bool:
    """True si el codigo es un error material confirmado, sin duda razonable."""
    return codigo in CODIGOS_RECHAZO


def categoria(codigo: str) -> Categoria:
    """Categoria de un codigo. Deterministica: el modelo no la elige."""
    return CATALOGO.get(codigo, ("", "integridad_datos"))[1]


def desviacion(
    codigo: str,
    campo: str,
    evidencia: str,
    declarado: str | None = None,
    esperado: str | None = None,
    monto_impactado: float = 0.0,
) -> dict:
    """Construye una desviacion: el material crudo del que el modelo hace un Hallazgo.

    La herramienta aporta codigo, campo, evidencia y monto (hechos). El modelo
    aporta severidad, titulo final, confianza y bloquea_pago (juicio).
    """
    return {
        "codigo": codigo,
        "titulo_sugerido": titulo(codigo),
        "categoria": categoria(codigo),
        "campo_afectado": campo,
        "valor_declarado": declarado,
        "valor_esperado": esperado,
        "evidencia": evidencia,
        "monto_impactado": monto_impactado,
    }
