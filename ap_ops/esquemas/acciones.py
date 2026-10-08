"""El plan de accion: el entregable central del sistema.

Un sistema que solo clasifica facturas no sirve de mucho. "Riesgo 72/100" no le
dice a nadie que hacer el lunes a la manana. Lo que el analista necesita es:
que hago, quien lo hace, para cuando, y con que evidencia lo justifico.

De ahi el requisito que atraviesa este modulo: TODO hallazgo produce al menos
una accion. `AccionPropuesta.codigo_hallazgo` es el vinculo, y
`PlanAccion.hallazgos_sin_accion` es el detector de la violacion. Los dos
campos juntos convierten "ante una observacion, proponer acciones" en un
assert, no en una esperanza depositada en el prompt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Catalogo cerrado de acciones. Es un Literal y no texto libre porque permite
#: enrutar: cada tipo tiene un responsable por defecto, un SLA tipico y un
#: runbook. Si fuera texto libre el modelo escribiria 40 variantes de
#: "contactar al proveedor" y nada de eso seria automatizable despues.
TipoAccion = Literal[
    "solicitar_nota_credito",
    "solicitar_oc_retroactiva",
    "solicitar_documento",
    "retener_pago",
    "verificar_cuenta_canal_alterno",
    "derivar_compras",
    "derivar_tesoreria",
    "derivar_contabilidad",
    "escalar_cumplimiento",
    "corregir_registro",
    "aprobar_con_condiciones",
    "sin_accion",
]

Responsable = Literal[
    "analista_cxp",
    "jefe_cxp",
    "compras",
    "tesoreria",
    "contabilidad",
    "cumplimiento",
    "proveedor",
]


class AccionPropuesta(BaseModel):
    """Una accion concreta para resolver un hallazgo."""

    id_accion: str
    codigo_hallazgo: str = Field(
        description="EL VINCULO. Debe ser el `codigo` de un Hallazgo existente "
        "de esta misma factura. Una accion sin hallazgo que la respalde es "
        "trabajo inventado."
    )

    accion: str = Field(
        description="En imperativo y cuantificada. 'Solicitar nota de credito por "
        "S/ 342.00 al proveedor' sirve; 'revisar el IGV' no sirve."
    )
    tipo_accion: TipoAccion
    responsable: Responsable
    prioridad: int = Field(ge=1, le=5, description="1 es lo mas urgente.")
    # ge=1 y NO gt=0: `gt` genera `exclusiveMinimum` en el JSON Schema, y el
    # `types.Schema` de Gemini lo rechaza (extra_forbidden) en tiempo de
    # llamada, no de import. Para enteros son equivalentes.
    sla_horas: int = Field(
        ge=1,
        description="Plazo. Sin plazo una accion no entra a ninguna cola de trabajo.",
    )

    justificacion: str = Field(description="Por que esta accion y no otra.")
    evidencia: str = Field(
        description="El dato duro que la sostiene, heredado del Hallazgo."
    )
    impacto_si_no_se_actua: str = Field(
        description="Obliga a declarar la consecuencia. Es lo que permite que un "
        "jefe priorice entre dos acciones de severidad parecida."
    )

    reversible: bool = Field(
        description="Si es False, exige confirmacion humana sin importar el monto. "
        "Liberar un pago no es reversible; pedir un documento si."
    )
    requiere_aprobacion_humana: bool
    bloquea_pago: bool
    monto_involucrado: float = 0.0


class PlanAccion(BaseModel):
    """Salida de `agente_plan_accion`.

    Las dos listas de control son el corazon del invariante:

      * `hallazgos_sin_accion`  -> hallazgos que quedaron huerfanos (falso negativo)
      * `acciones_sin_hallazgo` -> acciones sin respaldo (alucinacion)

    Ambas deben quedar vacias. Se las pedimos al modelo para que haga la
    verificacion explicitamente, pero NO confiamos en su palabra: el mismo
    chequeo se recalcula de forma determinista en `herramientas/` y en los
    tests dorados. Pedirselo sirve para que se autocorrija antes de responder;
    recalcularlo sirve para atraparlo cuando no lo hace.
    """

    id_factura: str
    iteracion: int = Field(ge=1, description="Vuelta del ciclo del critico.")
    acciones: list[AccionPropuesta] = Field(default_factory=list)
    hallazgos_sin_accion: list[str] = Field(
        default_factory=list, description="Debe quedar vacia."
    )
    acciones_sin_hallazgo: list[str] = Field(
        default_factory=list, description="Debe quedar vacia."
    )
    resumen: str


class VeredictoCritico(BaseModel):
    """Salida de `agente_critico`: puntua el plan contra una rubrica.

    El patron critico-generador existe porque el primer plan de un modelo tiende
    a ser genérico ("contactar al proveedor") y desproporcionado (pedir
    escalamiento a cumplimiento por una diferencia de S/ 3). Un segundo agente
    con la unica tarea de puntuarlo contra criterios explicitos corrige las dos
    cosas a un costo de una llamada.

    Las 4 dimensiones van desglosadas y no colapsadas en `puntaje_rubrica`
    porque `instrucciones_de_mejora` tiene que poder decir QUE dimension fallo.
    Un "65/100" sin desglose no es accionable para el replanteo.
    """

    aprobado: bool
    puntaje_rubrica: int = Field(ge=0, le=100)

    cobertura_hallazgos: int = Field(
        ge=0, le=100, description="Que % de hallazgos tiene al menos una accion."
    )
    especificidad: int = Field(
        ge=0, le=100, description="Las acciones son cuantificadas o genericas."
    )
    accionabilidad: int = Field(
        ge=0, le=100, description="Tienen responsable y SLA sensatos."
    )
    proporcionalidad: int = Field(
        ge=0,
        le=100,
        description="La severidad de la accion corresponde al monto y al riesgo. "
        "Penaliza tanto el exceso como la tibieza.",
    )

    observaciones: list[str] = Field(default_factory=list)
    instrucciones_de_mejora: str = Field(
        default="",
        description="Que corregir en la proxima iteracion. Vacio si aprobado.",
    )
