"""Consolidacion (determinista) y scoring de riesgo (LLM).

Son dos pasos muy distintos que conviene no confundir:

`nodo_consolidar` NO usa modelo. Deduplica, calcula el `puntaje_determinista` y
decide la ruta del grafo. El atajo que decide aqui —si no hay hallazgos, saltar
directo a la decision— es la mayor palanca de costo del sistema: ahorra tres
llamadas al modelo, y como la mayoria de las facturas estan limpias, es el
ahorro que hace viable procesar miles por mes.

`agente_scoring_riesgo` SI usa modelo, y aporta lo unico que una tabla no puede:
correlacion. Un proveedor nuevo es riesgo bajo. Un CCI que no coincide es riesgo
medio. Un monto 0.5% debajo del umbral es riesgo bajo. Los tres juntos son un
fraude en curso, y eso no sale de sumar los tres puntajes.
"""

from __future__ import annotations

from google.adk.agents import Context, LlmAgent
from google.adk.workflow import node
from google.genai import types

from .. import config
from ..esquemas import EvaluacionRiesgo
from ..herramientas import consolidar
from ._reintentos import REINTENTOS
from .validadores import CLAVES_VALIDADORES


@node(name="nodo_consolidar")
async def nodo_consolidar(ctx: Context) -> dict:
    """Deduplica los hallazgos de los 4 validadores, puntua y enruta. Sin LLM.

    Enruta con `ctx.route`:
      'sin_hallazgos'  -> atajo touchless, saltea scoring / plan / critico
      'con_hallazgos'  -> camino completo
    """
    hallazgos: list[dict] = []
    no_evaluables: list[str] = []

    for clave, nombre in CLAVES_VALIDADORES.items():
        resultado = ctx.state.get(clave)
        if not isinstance(resultado, dict):
            # El validador no dejo resultado: ni siquiera llego a responder.
            no_evaluables.append(nombre)
            continue
        if resultado.get("estado") == "no_evaluable":
            no_evaluables.append(nombre)
        hallazgos.extend(resultado.get("hallazgos") or [])

    cons = consolidar(
        id_factura=str(ctx.state.get("id_factura", "")),
        hallazgos=hallazgos,
        validadores_no_evaluables=no_evaluables,
    )

    ctx.state["consolidado"] = cons
    ctx.route = cons["ruta"]
    return cons


agente_scoring_riesgo = LlmAgent(
    name="agente_scoring_riesgo",
    model=config.MODELO_JUICIO,
    description="Correlaciona los hallazgos de los 4 validadores en un puntaje de riesgo.",
    instruction=(
        "Eres el jefe de Cuentas por Pagar evaluando el riesgo de una factura.\n\n"
        "ENTRADA (ya calculada, en el estado de la sesion)\n"
        "- Factura: {id_factura}, proveedor {nombre_proveedor}, "
        "total {moneda} {total_factura}\n"
        "- Consolidado deduplicado de los 4 validadores: {consolidado}\n\n"
        "TU TRABAJO ES CORRELACIONAR, NO RECONTAR\n"
        "El `puntaje_determinista` del consolidado ya suma las severidades. "
        "Repetir esa suma no aporta nada. Lo que aportas es el riesgo que surge "
        "de la COMBINACION de hallazgos, que puede ser muy superior a la suma:\n\n"
        "  - proveedor nuevo (FRA-NUE) + cuenta que no coincide (FRA-CTA) +\n"
        "    monto justo bajo el umbral (FRA-UMB) = fraude en curso, no tres\n"
        "    observaciones medias. Puntaje >= 85.\n"
        "  - sin orden de compra (TRV-OC) + proveedor nuevo = compra no\n"
        "    autorizada a un tercero sin historial. Mucho peor que cada parte.\n"
        "  - diferencia de IGV (TRI-IGV) + aritmetica mal (TRI-ARI) = el\n"
        "    comprobante esta mal emitido, no hay dolo. Puntaje moderado.\n\n"
        "REGLAS\n"
        "1. `hallazgos`: devuelve los MISMOS del consolidado. Puedes AJUSTAR la\n"
        "   severidad de uno si la correlacion lo justifica, y debes explicarlo\n"
        "   en `resumen_ejecutivo`. NO PUEDES agregar hallazgos nuevos ni\n"
        "   eliminar ninguno, ni cambiar su `codigo` o su `evidencia`.\n"
        "2. `puntaje_determinista`: copia el valor del consolidado tal cual.\n"
        "3. `desacuerdo_con_determinista`: true si tu `puntaje_riesgo` difiere\n"
        "   del determinista en mas de 20 puntos. Si es true,\n"
        "   `justificacion_desacuerdo` es OBLIGATORIO. Discrepar esta bien y es\n"
        "   util: dispara revision humana. Lo que no sirve es discrepar sin\n"
        "   decir por que.\n"
        "4. `codigos_bloqueantes`: los codigos de los hallazgos con\n"
        "   bloquea_pago=true.\n"
        "5. `monto_en_riesgo`: el monto realmente expuesto. Si varios hallazgos\n"
        "   apuntan al mismo dinero (p.ej. tres senales de fraude sobre la\n"
        "   misma factura), NO los sumes: es el total de la factura una sola vez.\n"
        "6. `resumen_ejecutivo`: maximo 4 lineas, en espanol, para que un jefe\n"
        "   decida sin leer el resto del expediente.\n"
        "7. `confianza`: tu confianza en esta evaluacion. Por debajo de 0.6 el\n"
        "   sistema la manda a revision humana, asi que usala con honestidad.\n\n"
        "Responde unicamente con el JSON del esquema."
    ),
    output_schema=EvaluacionRiesgo,
    output_key="evaluacion_riesgo",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1, max_output_tokens=8192
    ),
    # Lleva el retry generoso: NO esta en el ciclo del critico, asi que no se
    # multiplica. Sin esto, un 503 pasajero del modelo —que pasa, y pasa justo
    # aqui porque es la primera llamada a MODELO_JUICIO— tumba el pipeline
    # entero despues de haber gastado las 8 llamadas de los validadores.
    retry_config=REINTENTOS,
    timeout=config.TIMEOUT_AGENTE_JUICIO,
)

