"""La decision y el expediente: la salida final del pipeline.

`DecisionAP` es el unico modelo del sistema que NUNCA lo produce un LLM. Lo
produce `herramientas/decision.py` aplicando una tabla de reglas numerada
(R-01..R-08). Esa separacion es deliberada y no es negociable: un modelo puede
evaluar riesgo y proponer acciones, pero la regla que determina si el dinero se
mueve tiene que ser identica en cada corrida y reproducible por un auditor seis
meses despues.

`ExpedienteAP` es `output_schema` del Workflow, no de un agente. La distincion
importa: lo valida Pydantic al terminar el grafo, no Gemini como
`response_schema`. Por eso se permite el anidamiento profundo que tiene (factura
+ 4 resultados + consolidado + evaluacion + plan + veredicto + decision), que
seria demasiado para una respuesta estructurada de un modelo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .acciones import PlanAccion, Responsable, VeredictoCritico
from .factura import Factura
from .hallazgos import ConsolidadoValidacion, EvaluacionRiesgo
from .validadores import ResultadosValidacion

#: Cuatro resultados, no dos. "aprobar / rechazar" es una falsa dicotomia: la
#: mayoria de las facturas observadas no son fraude ni estan perfectas, son
#: pagables sujetas a una condicion. Sin ese estado intermedio el sistema
#: sobre-retiene y el area deja de confiar en el.
ResultadoDecision = Literal[
    "touchless_approve",
    "aprobar_con_condiciones",
    "retener",
    "rechazar",
]


class DecisionAP(BaseModel):
    """Resolucion final. Deterministica, producida por la tabla de reglas."""

    resultado: ResultadoDecision
    motivo: str
    condiciones: list[str] = Field(
        default_factory=list,
        description="Obligatorio si resultado='aprobar_con_condiciones'.",
    )
    monto_autorizado: float = Field(
        ge=0.0, description="Puede ser menor al total si hay un monto en disputa."
    )

    requiere_confirmacion_humana: bool

    regla_aplicada: str = Field(
        description="Id de la regla que decidio, p.ej. 'R-03'. Es LA trazabilidad: "
        "permite reconstruir el porque sin releer prompts ni logs del modelo."
    )
    umbral_usado: float = Field(
        description="El valor concreto del umbral que se comparo. Un 'R-03' sin "
        "el umbral no es reproducible si la config cambio."
    )
    aprobador_sugerido: Responsable


class ExpedienteAP(BaseModel):
    """El legajo completo de una factura. Salida terminal del Workflow.

    Lleva TODO el rastro intermedio, no solo la conclusion. Eso cuesta memoria,
    pero compra tres cosas que importan mas:

      * El memo de auditoria se renderiza desde aca sin volver a consultar nada.
      * `explicar_decision` del coordinador responde "por que retuviste X?" con
        una sola llamada al modelo en vez de re-ejecutar las 7 del pipeline.
      * Los tests dorados pueden afirmar sobre cualquier etapa intermedia, no
        solo sobre el resultado final.
    """

    id_expediente: str
    id_factura: str
    version_pipeline: str = Field(
        description="Para poder comparar expedientes entre versiones del grafo."
    )
    generado_en: str = Field(description="ISO 8601 con zona horaria.")

    factura: Factura
    resultados: ResultadosValidacion
    consolidado: ConsolidadoValidacion
    evaluacion: EvaluacionRiesgo
    plan_accion: PlanAccion
    veredicto_critico: VeredictoCritico
    decision: DecisionAP

    iteraciones_critico: int = Field(ge=0)
    artefacto_memo: str | None = Field(
        default=None, description="Nombre del artifact con el memo en Markdown."
    )

    llamadas_llm: int = Field(
        ge=0,
        description="Costo real de esta factura. Es la metrica que decide si el "
        "sistema es viable a 10.000 facturas por mes o solo en el demo.",
    )
    tokens_consumidos: int = Field(ge=0)
