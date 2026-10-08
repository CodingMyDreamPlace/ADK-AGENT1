"""El coordinador conversacional. Entry point 1 del diseno hibrido.

EL PUENTE
---------
ADK 2.11 deprecó `SequentialAgent` / `ParallelAgent` / `LoopAgent` a favor de
`Workflow`, pero `Workflow` es un `BaseNode` y NO un `BaseAgent`, asi que no se
puede poner en `LlmAgent.sub_agents`. El puente verificado en el codigo fuente
es `await tool_context.run_node(...)`:

    agents/context.py:432   async def run_node(self, node: NodeLike, node_input=None, ...)
    workflow/_graph.py:41   NodeLike = BaseNode | BaseTool | Callable | Literal["START"]
    workflow/_workflow.py:134   class Workflow(BaseNode)

Los cuatro riesgos que podian romperlo estan verificados como despejados: el
guard de `rerun_on_resume` pasa (un Context de tool se construye con node=None,
que da rerun_on_resume=True), el scheduler standalone esta explicitamente
soportado fuera de un grafo, los eventos hijos llegan al stream del Runner y a
la sesion, y un coordinador sin `sub_agents` mantiene `use_scheduler=False`.

LA LIMITACION, Y POR QUE NO LA PELEAMOS
---------------------------------------
Un interrupt de HITL *dentro* del Workflow no compone de vuelta a traves de un
FunctionTool: `NodeInterruptedError` escapa como error de tool
(`workflow/_dynamic_node_scheduler.py:745-747`).

Por eso el diseno pone el HITL en el COORDINADOR y no en el grafo. El workflow
nunca interrumpe: es un pipeline de analisis puro que siempre termina en un
`ExpedienteAP` con una *recomendacion*. Lo irreversible —liberar dinero— es una
tool del coordinador con `require_confirmation` (Fase 5). Eso coincide con la
regla de negocio en lugar de pelear con el framework.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.genai import types

from . import config


async def triar_factura(id_factura: str, tool_context: ToolContext) -> dict:
    """Ejecuta el pipeline completo de triaje de una factura y devuelve el expediente.

    Valida contra la orden de compra y la recepcion, revisa IGV y RUC, busca
    duplicados, verifica la cuenta bancaria y el contrato del proveedor, puntua
    el riesgo, propone acciones y aplica la regla de decision.

    Args:
      id_factura: Identificador de la factura. Acepta el formato de negocio
        (RUC-SERIE-NUMERO) o el nombre del fixture.
    """
    # Import perezoso: evita el ciclo coordinador -> flujo -> nodos, y acorta
    # el arranque en frio de Cloud Run (importar el grafo trae los 7 agentes).
    from .flujo import pipeline_ap

    expediente = await tool_context.run_node(
        pipeline_ap,
        {"id_factura": id_factura},
        run_id=f"triaje-{id_factura}",
    )

    if not isinstance(expediente, dict):
        return {
            "estado": "error",
            "mensaje": f"El pipeline no devolvio un expediente para {id_factura}.",
        }

    # Cache por factura. Es lo que permite que `explicar_decision` cueste UNA
    # llamada al modelo en lugar de las 7 de un retriaje.
    tool_context.state[f"expediente:{id_factura}"] = expediente
    tool_context.state[f"expediente:{expediente.get('id_factura')}"] = expediente
    tool_context.state["ultimo_expediente_id"] = expediente.get("id_factura")

    decision = expediente.get("decision", {})
    consolidado = expediente.get("consolidado", {})
    return {
        "estado": "completado",
        "id_factura": expediente.get("id_factura"),
        "decision": decision.get("resultado"),
        "regla_aplicada": decision.get("regla_aplicada"),
        "puntaje_riesgo": expediente.get("evaluacion", {}).get("puntaje_riesgo"),
        "hallazgos": [h.get("codigo") for h in consolidado.get("hallazgos", [])],
        "acciones": expediente.get("plan_accion", {}).get("acciones", []),
        "requiere_confirmacion_humana": decision.get("requiere_confirmacion_humana"),
        "memo": expediente.get("artefacto_memo"),
        "expediente": expediente,
    }


async def explicar_decision(id_factura: str, tool_context: ToolContext) -> dict:
    """Explica por que una factura ya triada recibio su decision, sin reprocesarla.

    Lee el expediente guardado en la sesion. NO vuelve a ejecutar el pipeline.

    Args:
      id_factura: Identificador de la factura ya triada.
    """
    exp = tool_context.state.get(f"expediente:{id_factura}")
    if exp is None:
        ultimo = tool_context.state.get("ultimo_expediente_id")
        return {
            "estado": "no_encontrado",
            "mensaje": (
                f"No hay expediente para {id_factura} en esta sesion. "
                f"Ejecuta triar_factura primero."
            ),
            "ultimo_triado": ultimo,
        }
    return {
        "estado": "ok",
        "id_factura": exp.get("id_factura"),
        "decision": exp.get("decision"),
        "evaluacion": exp.get("evaluacion"),
        "plan_accion": exp.get("plan_accion"),
        "consolidado": exp.get("consolidado"),
    }


async def replantear_plan(
    id_factura: str, instruccion: str, tool_context: ToolContext
) -> dict:
    """Regenera SOLO el plan de accion de una factura ya triada, sin revalidarla.

    Args:
      id_factura: Identificador de la factura ya triada.
      instruccion: Que cambiar en el plan. Por ejemplo "prioriza la
        verificacion bancaria" o "las acciones son demasiado genericas".
    """
    from .flujo import subflujo_replanteo

    exp = tool_context.state.get(f"expediente:{id_factura}")
    if exp is None:
        return {
            "estado": "no_encontrado",
            "mensaje": f"No hay expediente para {id_factura}. Ejecuta triar_factura primero.",
        }

    # Se resiembra el estado que los agentes del subflujo interpolan en sus
    # instrucciones. Sin esto, `agente_plan_accion` no tendria la evaluacion.
    tool_context.state["id_factura"] = exp.get("id_factura")
    tool_context.state["nombre_proveedor"] = exp.get("factura", {}).get("razon_social")
    tool_context.state["moneda"] = exp.get("factura", {}).get("moneda")
    tool_context.state["total_factura"] = exp.get("factura", {}).get("total")
    tool_context.state["factura"] = exp.get("factura")
    tool_context.state["consolidado"] = exp.get("consolidado")
    tool_context.state["evaluacion_riesgo"] = exp.get("evaluacion")
    tool_context.state["decision"] = exp.get("decision")
    tool_context.state["instruccion_replanteo"] = instruccion
    tool_context.state["instrucciones_mejora_critico"] = None
    tool_context.state["iter_critico"] = 0

    nuevo = await tool_context.run_node(
        subflujo_replanteo,
        {"id_factura": id_factura},
        run_id=f"replanteo-{id_factura}",
    )

    if isinstance(nuevo, dict):
        tool_context.state[f"expediente:{id_factura}"] = nuevo
        return {
            "estado": "replanteado",
            "plan_accion": nuevo.get("plan_accion"),
            "veredicto_critico": nuevo.get("veredicto_critico"),
        }
    return {"estado": "error", "mensaje": "El subflujo no devolvio un expediente."}


coordinador = LlmAgent(
    name="coordinador_ap_ops",
    model=config.MODELO_JUICIO,
    description=(
        "Centro de operaciones de Cuentas por Pagar: tria facturas, explica "
        "decisiones y replantea planes de accion."
    ),
    instruction=(
        "Eres el coordinador del centro de operaciones de Cuentas por Pagar. "
        "Hablas con analistas y jefes del area, en espanol peruano, conciso y "
        "accionable.\n\n"
        "CUANDO USAR CADA HERRAMIENTA\n"
        "- 'analiza / tria / revisa la factura X' -> `triar_factura`.\n"
        "- 'por que retuviste X', 'explicame la decision de X' ->\n"
        "  `explicar_decision`. NO vuelvas a triar: un retriaje cuesta siete\n"
        "  llamadas al modelo y el expediente ya esta en la sesion.\n"
        "- 'replantea el plan', 'las acciones son muy genericas' ->\n"
        "  `replantear_plan`.\n\n"
        "COMO RESUMIR UN TRIAJE (maximo 6 lineas)\n"
        "1. La decision y la regla que la produjo.\n"
        "2. El puntaje de riesgo.\n"
        "3. Los hallazgos mas graves, cada uno con su codigo.\n"
        "4. Las acciones propuestas, con responsable y SLA.\n"
        "Si la decision requiere confirmacion humana, decilo explicitamente.\n\n"
        "REGLAS QUE NO SE NEGOCIAN\n"
        "- Nunca inventes montos, RUCs, hallazgos ni codigos. Todo sale de las\n"
        "  herramientas. Si una herramienta no devolvio un dato, decilo: 'no\n"
        "  se pudo verificar', no una estimacion.\n"
        "- Nunca afirmes que un pago se ejecuto o se autorizo. Vos no mueves\n"
        "  dinero: el pipeline produce una RECOMENDACION y la autorizacion la\n"
        "  da una persona.\n"
        "- Cuando cites un hallazgo, usa su `codigo` y su `evidencia` textual.\n"
        "  La evidencia es lo que un auditor va a releer.\n"
        "- Nunca pidas ni muestres un numero de cuenta completo. Las cuentas\n"
        "  viajan enmascaradas a proposito.\n\n"
        "Ultima factura triada en esta sesion: {ultimo_expediente_id?}"
    ),
    tools=[triar_factura, explicar_decision, replantear_plan],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2, max_output_tokens=4096
    ),
    # Sin `sub_agents` a proposito: mantiene `use_scheduler=False` en el
    # runtime de nodos, que es la rama por la que `run_node` funciona de forma
    # simple desde el cuerpo de una FunctionTool.
)
