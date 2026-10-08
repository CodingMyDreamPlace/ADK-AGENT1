"""Nodos de cierre: la decision y el expediente. Deterministas, sin LLM.

`nodo_decision` aplica la tabla `R-01..R-08`. Es el unico lugar del sistema que
decide si el dinero se mueve, y por eso no lleva modelo.

`nodo_expediente` arma el `ExpedienteAP` y renderiza el memo de auditoria.

Sobre el ATAJO TOUCHLESS: cuando el consolidado no encuentra hallazgos, el grafo
saltea scoring, plan de accion y critico. Eso ahorra tres llamadas al modelo
—la mayor palanca de costo del sistema— pero deja tres claves sin escribir en
el estado, y `ExpedienteAP` las exige todas.

La solucion es sintetizarlas de forma determinista en lugar de hacerlas
opcionales en el esquema. Una factura limpia SI tiene una evaluacion de riesgo
(puntaje 0, sin hallazgos), SI tiene un plan de accion (vacio, porque no hay
nada que resolver) y SI tiene un veredicto del critico (aprobado, porque un
plan vacio para cero hallazgos es correcto). Mantener `ExpedienteAP` uniforme
evita que el memo, los tests, la cola de trabajo y el futuro esquema de
BigQuery tengan que manejar nulos.
"""

from __future__ import annotations

from google.adk.agents import Context
from google.adk.workflow import node

from .. import config
from ..esquemas import ExpedienteAP
from ..herramientas import aplicar_compuerta, render_memo_markdown
from ..herramientas._fechas import ahora_iso


def _evaluacion_sintetica(consolidado: dict) -> dict:
    """EvaluacionRiesgo para el camino touchless, sin llamar al modelo."""
    return {
        "id_factura": consolidado.get("id_factura", ""),
        "puntaje_riesgo": consolidado.get("puntaje_determinista", 0),
        "severidad_global": consolidado.get("severidad_maxima", "informativa"),
        "hallazgos": consolidado.get("hallazgos", []),
        "codigos_bloqueantes": consolidado.get("codigos_bloqueantes", []),
        "resumen_ejecutivo": (
            "Los cuatro validadores resolvieron conforme. No se detectaron "
            "desviaciones, por lo que el pipeline omitio el scoring de riesgo, "
            "el plan de accion y la revision del critico."
        ),
        "monto_en_riesgo": 0.0,
        "puntaje_determinista": consolidado.get("puntaje_determinista", 0),
        "desacuerdo_con_determinista": False,
        "justificacion_desacuerdo": None,
        "confianza": 1.0,
    }


def _plan_vacio(id_factura: str) -> dict:
    return {
        "id_factura": id_factura,
        "iteracion": 1,
        "acciones": [],
        "hallazgos_sin_accion": [],
        "acciones_sin_hallazgo": [],
        "resumen": "Sin hallazgos: no hay acciones que proponer.",
    }


def _veredicto_trivial() -> dict:
    """Un plan vacio para cero hallazgos tiene cobertura perfecta."""
    return {
        "aprobado": True,
        "puntaje_rubrica": 100,
        "cobertura_hallazgos": 100,
        "especificidad": 100,
        "accionabilidad": 100,
        "proporcionalidad": 100,
        "observaciones": [],
        "instrucciones_de_mejora": "",
    }


@node(name="nodo_decision")
async def nodo_decision(ctx: Context) -> dict:
    """Aplica la tabla de reglas R-01..R-08. Determinista."""
    consolidado = ctx.state.get("consolidado") or {}
    factura = ctx.state.get("factura") or {}
    evaluacion = ctx.state.get("evaluacion_riesgo")

    # En el camino touchless no hay evaluacion del modelo: se sintetiza.
    if not isinstance(evaluacion, dict):
        evaluacion = _evaluacion_sintetica(consolidado)
        ctx.state["evaluacion_riesgo"] = evaluacion

    # El puntaje que decide es el del modelo cuando existe, pero los HALLAZGOS
    # que se evaluan son los del consolidado determinista: el modelo puede
    # ajustar severidades, no puede hacer desaparecer un hallazgo.
    decision = aplicar_compuerta(
        puntaje_riesgo=int(evaluacion.get("puntaje_riesgo", 0)),
        hallazgos=consolidado.get("hallazgos", []),
        monto_total=float(factura.get("total", 0.0)),
        validadores_no_evaluables=consolidado.get("validadores_no_evaluables", []),
    )

    ctx.state["decision"] = decision
    return decision


@node(name="nodo_expediente")
async def nodo_expediente(ctx: Context) -> dict:
    """Arma el ExpedienteAP, renderiza el memo y lo guarda como artifact.

    Es el unico nodo TERMINAL del grafo, y por eso su salida es la salida del
    Workflow (`output_schema=ExpedienteAP`). El grafo admite un solo nodo
    terminal con output; si hubiera dos, ADK levanta
    `WorkflowConfigurationError`.
    """
    factura = ctx.state.get("factura") or {}
    consolidado = ctx.state.get("consolidado") or {}
    id_factura = str(ctx.state.get("id_factura", ""))

    evaluacion = ctx.state.get("evaluacion_riesgo") or _evaluacion_sintetica(consolidado)
    plan = ctx.state.get("plan_accion") or _plan_vacio(id_factura)
    veredicto = ctx.state.get("veredicto_critico") or _veredicto_trivial()
    decision = ctx.state.get("decision") or {}

    # El chequeo determinista del invariante gana sobre lo que declaro el
    # modelo. `compuerta_critico` ya lo calculo en el camino largo; en el
    # touchless las dos listas son vacias por construccion.
    plan = dict(plan)
    plan["hallazgos_sin_accion"] = ctx.state.get("huerfanos_reales") or []
    plan["acciones_sin_hallazgo"] = ctx.state.get("acciones_sin_respaldo_reales") or []

    resultados = {
        "tres_vias": ctx.state.get("val_tres_vias") or {},
        "tributario": ctx.state.get("val_tributario") or {},
        "duplicados_fraude": ctx.state.get("val_duplicados") or {},
        "proveedor_contrato": ctx.state.get("val_proveedor") or {},
    }

    expediente = {
        "id_expediente": f"EXP-{id_factura}",
        "id_factura": id_factura,
        "version_pipeline": config.VERSION_PIPELINE,
        "generado_en": ahora_iso(),
        "factura": factura,
        "resultados": resultados,
        "consolidado": {
            k: v
            for k, v in consolidado.items()
            if k not in ("ruta", "codigos", "codigos_bloqueantes")
        },
        "evaluacion": evaluacion,
        "plan_accion": plan,
        "veredicto_critico": veredicto,
        "decision": decision,
        "iteraciones_critico": int(ctx.state.get("iter_critico", 0)),
        "artefacto_memo": None,
        "llamadas_llm": int(ctx.state.get("llamadas_llm", 0)),
        "tokens_consumidos": int(ctx.state.get("tokens_consumidos", 0)),
    }

    # --- memo como artifact --------------------------------------------------
    nombre_memo = f"memos/{id_factura}.md"
    try:
        memo = render_memo_markdown(expediente)
        from google.genai import types

        await ctx.save_artifact(
            nombre_memo,
            types.Part.from_bytes(data=memo.encode("utf-8"), mime_type="text/markdown"),
        )
        expediente["artefacto_memo"] = nombre_memo
    except Exception as e:  # noqa: BLE001
        # Sin ArtifactService configurado, `save_artifact` levanta ValueError.
        # El memo es importante pero no es la razon de ser del pipeline: si
        # falla, el expediente sale igual y queda la senal para el eje ops.
        ctx.state["error_artifact_memo"] = repr(e)

    # Validacion dura: si el expediente no cuadra con el esquema, queremos el
    # error aqui y no mas tarde en la cola de trabajo o en BigQuery.
    ExpedienteAP.model_validate(expediente)
    return expediente
