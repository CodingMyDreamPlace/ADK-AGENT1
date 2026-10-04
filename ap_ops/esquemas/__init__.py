"""Esquemas de AP Ops.

Reexporta todo para que el resto del paquete importe de un solo lugar
(`from ..esquemas import Factura, Hallazgo`) en vez de conocer la division
interna en modulos.

Flujo de datos a traves de estos tipos:

    Factura + OrdenCompra + NotaRecepcion + ProveedorMaestro   (datos/, en reposo)
        -> ContextoValidacion                                  (nodo_intake)
        -> ResultadoTresVias | ResultadoTributario |            (4 validadores,
           ResultadoDuplicadosFraude | ResultadoProveedorContrato   en paralelo)
        -> ResultadosValidacion                                (JoinNode)
        -> ConsolidadoValidacion                               (determinista)
        -> EvaluacionRiesgo                                    (LLM)
        -> PlanAccion  <-> VeredictoCritico                    (ciclo acotado)
        -> DecisionAP                                          (determinista, R-01..R-08)
        -> ExpedienteAP                                        (salida del Workflow)

En paralelo a todo eso, y sin tocar el camino principal:

    SenalOps -> RecomendacionOps -> RegistroAuditoria          (plugin de auditoria)
"""

from __future__ import annotations

from .acciones import (
    AccionPropuesta,
    PlanAccion,
    Responsable,
    TipoAccion,
    VeredictoCritico,
)
from .comunes import (
    NOMBRE_COMPROBANTE,
    ORDEN_SEVERIDAD,
    Categoria,
    DatoVerificado,
    Moneda,
    Severidad,
    TipoComprobante,
    severidad_maxima,
)
from .expediente import DecisionAP, ExpedienteAP, ResultadoDecision
from .factura import (
    CondicionPago,
    ContextoValidacion,
    CuentaBancaria,
    Factura,
    LineaFactura,
    NotaRecepcion,
    OrdenCompra,
    ProveedorMaestro,
)
from .hallazgos import ConsolidadoValidacion, EvaluacionRiesgo, Hallazgo
from .ops import (
    RecomendacionOps,
    RegistroAuditoria,
    SenalOps,
    TipoAccionOps,
    TipoSenal,
)
from .validadores import (
    CoincidenciaDuplicado,
    EstadoValidador,
    ResultadoDuplicadosFraude,
    ResultadoProveedorContrato,
    ResultadosValidacion,
    ResultadoTresVias,
    ResultadoTributario,
    ResultadoValidadorBase,
)

__all__ = [
    # comunes
    "Severidad",
    "ORDEN_SEVERIDAD",
    "severidad_maxima",
    "Moneda",
    "TipoComprobante",
    "NOMBRE_COMPROBANTE",
    "Categoria",
    "DatoVerificado",
    # factura y maestros
    "LineaFactura",
    "CuentaBancaria",
    "CondicionPago",
    "Factura",
    "OrdenCompra",
    "NotaRecepcion",
    "ProveedorMaestro",
    "ContextoValidacion",
    # hallazgos
    "Hallazgo",
    "ConsolidadoValidacion",
    "EvaluacionRiesgo",
    # validadores
    "EstadoValidador",
    "ResultadoValidadorBase",
    "ResultadoTresVias",
    "ResultadoTributario",
    "CoincidenciaDuplicado",
    "ResultadoDuplicadosFraude",
    "ResultadoProveedorContrato",
    "ResultadosValidacion",
    # acciones
    "TipoAccion",
    "Responsable",
    "AccionPropuesta",
    "PlanAccion",
    "VeredictoCritico",
    # expediente
    "ResultadoDecision",
    "DecisionAP",
    "ExpedienteAP",
    # ops
    "TipoSenal",
    "TipoAccionOps",
    "SenalOps",
    "RecomendacionOps",
    "RegistroAuditoria",
]
