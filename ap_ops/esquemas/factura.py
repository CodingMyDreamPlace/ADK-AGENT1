"""Documentos de entrada: la factura y los maestros contra los que se valida.

Estos modelos describen datos EN REPOSO (los fixtures JSON de `datos/`, que en
Fase 7 pasan a ser filas de BigQuery). A diferencia de los esquemas de salida
del LLM, aca usamos `extra="forbid"`: si un fixture tiene un campo mal escrito
queremos que explote al cargarlo, no que lo ignore en silencio.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .comunes import Moneda, TipoComprobante

CondicionPago = Literal["contado", "credito_15", "credito_30", "credito_45", "credito_60"]


class LineaFactura(BaseModel):
    """Una linea de detalle. La usan tambien la OC y la nota de recepcion.

    Compartir el modelo entre los tres documentos es lo que hace posible el
    3-way match linea por linea: comparar `cantidad` y `precio_unitario` del
    mismo `codigo_articulo` en los tres lados.
    """

    model_config = ConfigDict(extra="forbid")

    numero_linea: int = Field(ge=1)
    descripcion: str
    codigo_articulo: str | None = Field(
        default=None,
        description="Clave de conciliacion con la OC. Si falta, el 3-way match "
        "cae a comparar por descripcion, que es mucho mas debil.",
    )
    cantidad: float
    unidad_medida: str = Field(default="NIU", description="Codigo SUNAT. NIU = unidad.")
    precio_unitario: float
    importe: float = Field(
        description="cantidad * precio_unitario. Se recalcula en la herramienta: "
        "que venga en el documento no significa que este bien."
    )
    centro_costo: str | None = None


class CuentaBancaria(BaseModel):
    """Cuenta de abono.

    El CCI (Codigo de Cuenta Interbancario, 20 digitos) es el campo critico:
    comparar el CCI declarado contra el registrado en el maestro de proveedores
    es la deteccion de fraude de mayor rendimiento en todo el pipeline. El
    fraude clasico de AP no es inventar una factura, es interceptar una real y
    cambiarle la cuenta de destino.
    """

    model_config = ConfigDict(extra="forbid")

    banco: str
    tipo_cuenta: Literal["corriente", "ahorros", "detracciones"]
    numero_cuenta: str
    cci: str | None = Field(default=None, min_length=20, max_length=20)
    titular: str = Field(
        description="Debe coincidir con la razon social. Una cuenta a nombre de "
        "una persona natural para un proveedor con RUC 20 es sospechosa."
    )
    moneda_cuenta: Moneda


class Factura(BaseModel):
    """El comprobante recibido, ya normalizado desde el OCR."""

    model_config = ConfigDict(extra="forbid")

    id_factura: str = Field(
        description="Clave canonica: '{ruc_proveedor}-{serie}-{numero}'. "
        "Es tambien la huella base para detectar duplicados exactos."
    )
    ruc_proveedor: str = Field(min_length=11, max_length=11)
    razon_social: str
    tipo_comprobante: TipoComprobante
    serie: str
    numero: str
    fecha_emision: str = Field(description="ISO 8601, YYYY-MM-DD.")
    fecha_vencimiento: str | None = None

    moneda: Moneda
    tipo_cambio: float | None = Field(
        default=None,
        description="Obligatorio si moneda != PEN. Sin el no se puede comparar "
        "contra umbrales de aprobacion, que estan en soles.",
    )
    subtotal: float
    igv: float
    otros_cargos: float = 0.0
    total: float

    orden_compra: str | None = Field(
        default=None,
        description="Si es None, no hay 3-way match posible: es un hallazgo en si mismo.",
    )
    guia_remision: str | None = None
    lineas: list[LineaFactura] = Field(min_length=1)

    cuenta_bancaria: CuentaBancaria
    condicion_pago: CondicionPago

    detraccion_aplica: bool | None = None
    codigo_detraccion: str | None = Field(
        default=None, description="Codigo del Anexo 3 de SUNAT, p.ej. '037' servicios."
    )

    observaciones_ocr: list[str] = Field(
        default_factory=list,
        description="Lo que el OCR no pudo leer con certeza. Alimenta el estado "
        "'no_evaluable' en vez de forzar al modelo a adivinar.",
    )
    confianza_ocr: float | None = Field(default=None, ge=0.0, le=1.0)


class OrdenCompra(BaseModel):
    """Orden de compra: el lado "autorizado" del 3-way match."""

    model_config = ConfigDict(extra="forbid")

    numero_oc: str
    ruc_proveedor: str
    fecha_emision: str
    moneda: Moneda
    monto_autorizado: float
    monto_consumido: float = Field(
        default=0.0,
        description="Lo ya facturado contra esta OC. monto_autorizado menos esto "
        "es el disponible: facturar por encima es sobrefacturacion.",
    )
    estado: Literal["abierta", "cerrada", "anulada"]
    lineas: list[LineaFactura]
    comprador: str
    centro_costo: str


class NotaRecepcion(BaseModel):
    """Nota de recepcion de almacen: el lado "recibido" del 3-way match.

    Es el tercero de los tres vertices. Sin ella, una factura puede cuadrar
    perfecto con la OC y aun asi corresponder a mercaderia que nunca llego.
    """

    model_config = ConfigDict(extra="forbid")

    numero_nr: str
    numero_oc: str
    fecha_recepcion: str
    almacen: str
    recibido_por: str
    lineas: list[LineaFactura]
    estado: Literal["parcial", "completa", "con_observaciones"]


class ProveedorMaestro(BaseModel):
    """Ficha del proveedor. Fuente de verdad para fraude y para contrato."""

    model_config = ConfigDict(extra="forbid")

    ruc: str = Field(min_length=11, max_length=11)
    razon_social: str
    estado: Literal["habilitado", "suspendido", "bloqueado", "nuevo"]
    fecha_alta: str
    cuentas_registradas: list[CuentaBancaria] = Field(
        description="Lista, no una sola: un proveedor legitimo puede tener cuenta "
        "corriente y cuenta de detracciones. El CCI declarado debe coincidir con "
        "ALGUNA de estas."
    )
    condicion_pago_contractual: CondicionPago
    limite_contrato: float | None = None
    monto_consumido_contrato: float = 0.0
    categoria_riesgo: Literal["bajo", "medio", "alto"] = "bajo"
    es_agente_retencion: bool = False


class ContextoValidacion(BaseModel):
    """Salida de `nodo_intake`: lo que los 4 validadores reciben como node_input.

    Los flags `*_hallada` existen para que un validador pueda distinguir
    "lo verifique y esta mal" de "no pude verificarlo". Esa diferencia es la que
    sostiene el estado `no_evaluable` y evita que el modelo invente datos cuando
    un lookup falla.
    """

    model_config = ConfigDict(extra="forbid")

    id_factura: str
    factura: Factura
    orden_compra_hallada: bool
    nota_recepcion_hallada: bool
    proveedor_hallado: bool
    alertas_normalizacion: list[str] = Field(default_factory=list)
