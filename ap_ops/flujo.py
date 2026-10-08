"""El grafo. Aqui se define el ORDEN del pipeline.

La diferencia con un agente que tiene todas las herramientas es exactamente
esta: el orden lo decide el grafo, no el modelo. Nunca le preguntamos al LLM
"que haces ahora" — las aristas ya lo dicen. Un modelo al que se le piden doce
cosas hace nueve, y no falla ruidosamente: devuelve algo plausible al que le
falta la mitad.

SINTAXIS DE `edges` EN ADK 2.11
  (a, b)                   una arista a -> b
  (a, (b, c, d))            fan-out: tres aristas desde a
  ((b, c, d), e)            fan-in: tres aristas hacia e
  (a, b, c)                 cadena: a -> b -> c
  Edge(from_node=a, to_node=b, route="x")   arista condicional

`JoinNode` es distinto de un fan-in comun: tiene
`_requires_all_predecessors = True`, asi que espera a que TODOS sus
predecesores completen y dispara una sola vez con
`node_input = {nombre_nodo: salida}`.
"""

from __future__ import annotations

from google.adk.workflow import START, Edge, JoinNode, Workflow

from . import config
from .esquemas import ExpedienteAP
from .nodos.cierre import nodo_decision, nodo_expediente
from .nodos.intake import nodo_intake
from .nodos.plan_accion import agente_critico, agente_plan_accion, compuerta_critico
from .nodos.scoring import agente_scoring_riesgo, nodo_consolidar
from .nodos.validadores import VALIDADORES

#: Barrera de sincronizacion del fan-out. Espera a los cuatro validadores.
join_validadores = JoinNode(name="join_validadores")


pipeline_ap = Workflow(
    name="pipeline_ap",
    description=(
        "Triaje autonomo de excepciones de facturas de Cuentas por Pagar: "
        "valida, puntua el riesgo, propone acciones y decide."
    ),
    # Los cuatro validadores en paralelo. La etapa tarda lo que tarda UNA
    # llamada, no cuatro. No reduce el costo, reduce la latencia.
    max_concurrency=config.MAX_CONCURRENCIA,
    timeout=config.TIMEOUT_PIPELINE,
    # nodo_expediente es el unico nodo terminal: su salida es la del Workflow.
    output_schema=ExpedienteAP,
    edges=[
        # START es obligatorio: ADK valida que todos los nodos sean alcanzables
        # desde el. Registra el `node_input` del Workflow y siembra sus
        # sucesores; nunca se ejecuta. Sus aristas no pueden llevar `route`.
        (START, nodo_intake),
        (nodo_intake, VALIDADORES),
        (VALIDADORES, join_validadores),
        (join_validadores, nodo_consolidar),
        # --- el atajo touchless: la mayor palanca de costo del sistema -------
        # Sin hallazgos, se saltean scoring + plan + critico. Son 3 llamadas al
        # modelo que no se gastan, y como la mayoria de las facturas estan
        # limpias, es el ahorro que hace viable procesar miles por mes.
        Edge(from_node=nodo_consolidar, to_node=nodo_decision, route="sin_hallazgos"),
        Edge(
            from_node=nodo_consolidar,
            to_node=agente_scoring_riesgo,
            route="con_hallazgos",
        ),
        # --- el camino largo -------------------------------------------------
        (agente_scoring_riesgo, agente_plan_accion, agente_critico, compuerta_critico),
        # El ciclo del critico. Es legal porque al menos una arista del ciclo
        # esta enrutada: `_detect_unconditional_cycles` rechaza los ciclos
        # 100% incondicionales. El TOPE de iteraciones no lo pone el framework
        # (ADK 2.11 no tiene ninguno), lo pone `compuerta_critico`.
        Edge(from_node=compuerta_critico, to_node=agente_plan_accion, route="reintentar"),
        Edge(from_node=compuerta_critico, to_node=nodo_decision, route="continuar"),
        # --- cierre ----------------------------------------------------------
        (nodo_decision, nodo_expediente),
    ],
)


#: Subflujo de replanteo: regenera SOLO el plan de accion.
#:
#: Lo usa la herramienta `replantear_plan` del coordinador cuando el usuario
#: dice "replantea el plan, prioriza la verificacion bancaria". Reejecutar
#: `pipeline_ap` completo costaria 7+ llamadas para rehacer un trabajo que
#: necesita 2 o 3: las validaciones no cambiaron, solo el plan.
subflujo_replanteo = Workflow(
    name="subflujo_replanteo",
    description="Regenera el plan de accion sin revalidar la factura.",
    timeout=config.TIMEOUT_PIPELINE,
    edges=[
        (START, agente_plan_accion),
        (agente_plan_accion, agente_critico, compuerta_critico),
        Edge(from_node=compuerta_critico, to_node=agente_plan_accion, route="reintentar"),
        Edge(from_node=compuerta_critico, to_node=nodo_expediente, route="continuar"),
    ],
)

