"""Salidas de los 4 validadores que corren en paralelo.

Cada validador tiene su propio `output_schema` y su propio `output_key`. Eso no
es decoracion: es la defensa contra colisiones de estado. En ADK 2.11 el fan-out
de un Workflow aisla los EVENTOS por sub-branch, pero `ctx.state` sigue siendo
un unico diccionario de sesion. Dos nodos paralelos escribiendo la misma clave
es last-writer-wins, no deterministico. Claves distintas nunca compiten.

Ademas de los hallazgos, cada esquema expone los NUMEROS CRUDOS que la
herramienta calculo (igv_declarado vs igv_calculado, diferencia_porcentaje,
monto_disponible...). Eso cumple dos funciones: el memo de auditoria los cita
textualmente, y los tests dorados pueden afirmar sobre ellos sin depender de
como el modelo redacto el `resumen`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .comunes import DatoVerificado
from .hallazgos import Hallazgo

#: Tri-estado deliberado. `no_evaluable` NO es un error del sistema: es la
#: respuesta correcta cuando falta un insumo. Sin este estado el modelo queda
#: forzado a elegir entre "conforme" (falso negativo peligroso) e inventar
#: cifras para justificar un "observado".
EstadoValidador = Literal["conforme", "observado", "no_evaluable"]


class ResultadoValidadorBase(BaseModel):
    """Campos comunes a los 4 validadores."""

    validador: str
    estado: EstadoValidador
    hallazgos: list[Hallazgo] = Field(default_factory=list)
    resumen: str = Field(
        description="2 o 3 lineas. Si estado='no_evaluable', debe explicar que falto."
    )
    datos_verificados: list[DatoVerificado] = Field(default_factory=list)


class ResultadoTresVias(ResultadoValidadorBase):
    """Conciliacion factura <-> orden de compra <-> nota de recepcion.

    El 3-way match es el control mas antiguo de AP y sigue siendo el que atrapa
    mas plata: responde "lo pedimos?", "lo recibimos?" y "nos cobran lo
    acordado?". Fallar cualquiera de los tres vertices es material.
    """

    orden_compra_encontrada: bool
    nota_recepcion_encontrada: bool
    lineas_conciliadas: int = Field(ge=0)
    lineas_con_diferencia: int = Field(ge=0)
    diferencia_cantidad_total: float
    diferencia_monto: float
    diferencia_porcentaje: float
    dentro_tolerancia: bool
    tolerancia_aplicada_pct: float = Field(
        description="Que tolerancia se uso. Explicitarla es lo que hace "
        "defendible un 'dentro_tolerancia=True' ante un auditor."
    )


class ResultadoTributario(ResultadoValidadorBase):
    """IGV, aritmetica, RUC, tipo de comprobante, periodo y detraccion.

    `igv_declarado` e `igv_calculado` van los dos, siempre, incluso cuando
    coinciden. El delta es la evidencia; un booleano solo no sirve para el memo.
    """

    ruc_valido: bool = Field(description="Digito verificador modulo 11.")
    ruc_tipo: Literal["10", "15", "17", "20", "desconocido"] = Field(
        description="Los dos primeros digitos. 20 = persona juridica, "
        "10 = persona natural con negocio. Un RUC 10 facturando montos altos "
        "a una empresa merece atencion."
    )
    igv_declarado: float
    igv_calculado: float
    diferencia_igv: float
    aritmetica_correcta: bool = Field(
        description="subtotal + igv + otros_cargos == total, y sum(lineas) == subtotal."
    )
    tipo_comprobante_valido: bool = Field(
        description="False para una boleta (03) en el flujo de AP: no da credito fiscal."
    )
    periodo_valido: bool
    dias_antiguedad: int = Field(
        description="Una factura de hace 14 meses ya no es deducible."
    )
    detraccion_requerida: bool
    porcentaje_detraccion: float | None = None
    cuenta_detraccion_presente: bool


class CoincidenciaDuplicado(BaseModel):
    """Una factura previa que se parece a la actual.

    `tipo_coincidencia` esta ordenado de mas fuerte a mas debil. Una coincidencia
    'exacta' es un duplicado; 'monto_proveedor' es apenas una pista que puede ser
    perfectamente legitima (un abono mensual fijo).
    """

    id_factura_previa: str
    tipo_coincidencia: Literal["exacta", "huella", "monto_fecha", "monto_proveedor"]
    similitud: float = Field(ge=0.0, le=1.0)
    fecha_registro_previa: str
    estado_pago_previa: str


class ResultadoDuplicadosFraude(ResultadoValidadorBase):
    """Duplicados y senales de fraude.

    Las cuentas van ENMASCARADAS (`cuenta_declarada_enmascarada`), nunca
    completas. Este resultado viaja al prompt de varios agentes, queda en el
    estado de sesion, se persiste en la base y se exporta en trazas de OTel.
    Un numero de cuenta completo no deberia estar en ninguno de esos lugares.
    Para la comparacion exacta basta un booleano.
    """

    huella: str = Field(description="Hash de ruc+serie+numero+total+fecha.")
    duplicados_detectados: list[CoincidenciaDuplicado] = Field(default_factory=list)

    cuenta_bancaria_coincide: bool = Field(
        description="El CCI declarado coincide con ALGUNA cuenta registrada del "
        "proveedor. False es la senal de fraude de mayor rendimiento del pipeline."
    )
    cuenta_registrada_enmascarada: str | None = None
    cuenta_declarada_enmascarada: str

    proveedor_nuevo: bool
    dias_desde_alta_proveedor: int | None = None

    monto_bajo_umbral_sospechoso: bool = Field(
        description="Monto apenas por debajo de un umbral de aprobacion. El "
        "fraccionamiento deliberado para esquivar una firma es un patron clasico."
    )
    distancia_al_umbral: float | None = None
    indicadores_fraude: list[str] = Field(default_factory=list)


class ResultadoProveedorContrato(ResultadoValidadorBase):
    """Habilitacion del proveedor y limites contractuales."""

    proveedor_habilitado: bool
    estado_proveedor: str
    condicion_pago_declarada: str
    condicion_pago_contractual: str | None = None
    condicion_pago_coincide: bool = Field(
        description="Una factura a contado contra un contrato a 60 dias adelanta "
        "capital de trabajo sin autorizacion. Es un hallazgo financiero real, "
        "aunque el monto cuadre perfecto."
    )
    limite_contrato: float | None = None
    monto_consumido: float | None = None
    monto_disponible: float | None = None
    excede_contrato: bool
    es_agente_retencion: bool


class ResultadosValidacion(BaseModel):
    """Forma tipada del dict que produce el JoinNode.

    El `JoinNode` entrega `{nombre_del_nodo: output}`. Este modelo le da nombre y
    tipo a esa estructura para que `nodo_consolidar` y el `ExpedienteAP` no
    trabajen contra un diccionario anonimo.
    """

    tres_vias: ResultadoTresVias
    tributario: ResultadoTributario
    duplicados_fraude: ResultadoDuplicadosFraude
    proveedor_contrato: ResultadoProveedorContrato
