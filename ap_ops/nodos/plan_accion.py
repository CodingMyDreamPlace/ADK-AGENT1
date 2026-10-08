"""Plan de accion, agente critico y la compuerta que acota el ciclo.

Aqui vive el entregable central del sistema. Un numero de riesgo no le dice a
nadie que hacer el lunes a la manana; una accion con responsable, plazo y
evidencia si.

El patron generador-critico existe por una razon empirica: el primer plan de un
modelo tiende a ser generico ("contactar al proveedor") y desproporcionado
(escalar a cumplimiento por una diferencia de S/ 3). Un segundo agente cuya
unica tarea es puntuarlo contra criterios explicitos corrige las dos cosas por
el costo de una llamada.

ADVERTENCIA CRITICA: ADK 2.11 NO tiene cap de iteraciones en los ciclos del
grafo (verificado: no existe `max_iterations` ni `max_visits` en todo
`google/adk/workflow/`). `compuerta_critico` es la UNICA defensa contra un
ciclo infinito, y por lo tanto contra gasto ilimitado. Por eso no lleva LLM.
"""

from __future__ import annotations

from google.adk.agents import Context, LlmAgent
from google.adk.workflow import node
from google.genai import types

from .. import config
from ..esquemas import PlanAccion, VeredictoCritico
from ._reintentos import REINTENTOS_EN_CICLO

agente_plan_accion = LlmAgent(
    name="agente_plan_accion",
    model=config.MODELO_JUICIO,
    description="Convierte cada hallazgo en acciones concretas con responsable y SLA.",
    instruction=(
        "Eres analista senior de Cuentas por Pagar y tienes que dejarle a tu "
        "equipo un plan ejecutable para la factura {id_factura} de "
        "{nombre_proveedor} por {moneda} {total_factura}.\n\n"
        "ENTRADA\n"
        "- Evaluacion de riesgo con los hallazgos: {evaluacion_riesgo}\n"
        "- Instruccion adicional del usuario, si hay: {instruccion_replanteo?}\n"
        "- Observaciones del critico de la vuelta anterior, si hay: "
        "{instrucciones_mejora_critico?}\n\n"
        "LA REGLA QUE DEFINE TU TRABAJO\n"
        "TODO hallazgo debe tener al menos una accion, y TODA accion debe "
        "apuntar a un hallazgo existente mediante `codigo_hallazgo`. Al "
        "terminar, verificalo vos mismo y reporta el resultado en\n"
        "`hallazgos_sin_accion` y `acciones_sin_hallazgo`. Las dos listas "
        "deben quedar VACIAS. El sistema recalcula este chequeo de forma "
        "determinista, asi que mentir no sirve de nada.\n\n"
        "COMO SE ESCRIBE UNA ACCION QUE SIRVE\n"
        "Mal:  'Revisar el IGV de la factura'\n"
        "Bien: 'Solicitar nota de credito por S/ 336.00 al proveedor por "
        "diferencia de IGV'\n"
        "La diferencia es que la segunda es imperativa, cuantificada y "
        "verificable: alguien puede decir si se hizo o no.\n\n"
        "COMO ELEGIR `tipo_accion` Y `responsable`\n"
        "- diferencia de IGV o monto -> solicitar_nota_credito / analista_cxp\n"
        "- factura sin orden de compra -> solicitar_oc_retroactiva / compras\n"
        "- cuenta bancaria que no coincide -> verificar_cuenta_canal_alterno /\n"
        "  tesoreria, Y escalar_cumplimiento / cumplimiento. La verificacion\n"
        "  debe decir explicitamente que se llame al contacto REGISTRADO del\n"
        "  proveedor, no al que figura en el correo que trajo la factura.\n"
        "- duplicado -> retener_pago / jefe_cxp\n"
        "- proveedor suspendido o no registrado -> derivar_compras / compras\n"
        "- excede el limite de contrato -> derivar_compras / compras (adenda)\n"
        "- condicion de pago distinta -> corregir_registro / contabilidad\n"
        "- OCR ilegible -> solicitar_documento / proveedor\n\n"
        "SLA SEGUN SEVERIDAD\n"
        "critica 4 h · alta 24 h · media 48 h · baja 72 h.\n"
        "`prioridad`: 1 para critica, 2 alta, 3 media, 4 baja.\n\n"
        "CAMPOS QUE LA GENTE SUELE LLENAR MAL\n"
        "- `evidencia`: heredala del hallazgo. No la reescribas.\n"
        "- `impacto_si_no_se_actua`: la consecuencia concreta, no una\n"
        "  generalidad. 'Reparo tributario por S/ 336 de credito fiscal\n"
        "  indebido' sirve; 'problemas con SUNAT' no.\n"
        "- `reversible`: false para todo lo que mueva dinero o se comunique\n"
        "  al exterior. Pedir un documento es reversible; liberar un pago no.\n"
        "- `requiere_aprobacion_humana`: true si no es reversible, o si el\n"
        f"  monto supera S/ {config.UMBRAL_MONTO_HITL:,.2f}.\n"
        "- `bloquea_pago`: heredalo del hallazgo que la origina.\n\n"
        "PROPORCIONALIDAD\n"
        "No escales a cumplimiento por una diferencia de centavos, y no pidas "
        "una nota de credito por un problema de fraude. La accion tiene que "
        "corresponder al tamano del problema.\n\n"
        "`iteracion`: 1 en la primera pasada; si hay observaciones del critico, "
        "incrementala.\n\n"
        "Responde unicamente con el JSON del esquema."
    ),
    output_schema=PlanAccion,
    output_key="plan_accion",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2, max_output_tokens=8192
    ),
    # Retry ACOTADO: este nodo esta dentro del ciclo del critico, asi que el
    # peor caso son 2 intentos x MAX_ITER_CRITICO. Y solo ante excepciones
    # transitorias: un 503 no debe tumbar el pipeline, pero un error de logica
    # tampoco debe reintentarse en cada vuelta.
    retry_config=REINTENTOS_EN_CICLO,
    timeout=config.TIMEOUT_AGENTE_JUICIO,
)


agente_critico = LlmAgent(
    name="agente_critico",
    model=config.MODELO_RAPIDO,
    description="Puntua el plan de accion contra una rubrica de 4 dimensiones.",
    instruction=(
        "Eres auditor interno revisando un plan de accion de Cuentas por Pagar. "
        "Tu trabajo NO es proponer acciones: es puntuar el plan que te dan y "
        "decir que corregir.\n\n"
        "ENTRADA\n"
        "- Hallazgos: {evaluacion_riesgo}\n"
        "- Plan propuesto: {plan_accion}\n\n"
        "RUBRICA, 0 a 100 cada dimension\n\n"
        "1. `cobertura_hallazgos` — que porcentaje de los hallazgos tiene al "
        "menos una accion que lo referencie por `codigo_hallazgo`. Esto es "
        "aritmetica: contalo, no lo estimes. Si hay 3 hallazgos y 2 tienen "
        "accion, son 67.\n\n"
        "2. `especificidad` — las acciones son imperativas y cuantificadas, o "
        "genericas. Penaliza duro 'revisar', 'analizar', 'verificar' sin objeto "
        "ni monto.\n\n"
        "3. `accionabilidad` — el `responsable` es el area que realmente puede "
        "ejecutar la accion, y el `sla_horas` es sensato para la severidad. Un "
        "SLA de 72 h para un fraude critico es un error de accionabilidad.\n\n"
        "4. `proporcionalidad` — la severidad de la accion corresponde al monto "
        "y al riesgo. Penaliza las DOS direcciones: el exceso (escalar a "
        "cumplimiento por S/ 3) y la tibieza (pedir una nota de credito ante "
        "un indicio de fraude).\n\n"
        "VEREDICTO\n"
        "- `puntaje_rubrica`: el promedio de las cuatro.\n"
        f"- `aprobado`: true si `cobertura_hallazgos` >= "
        f"{config.UMBRAL_COBERTURA_CRITICO} Y `puntaje_rubrica` >= 70.\n"
        "- `observaciones`: una linea por problema concreto encontrado.\n"
        "- `instrucciones_de_mejora`: si no esta aprobado, deci EXACTAMENTE que "
        "cambiar y en cual accion. Vacio si esta aprobado.\n\n"
        "Se exigente pero no perfeccionista: el plan tiene que ser ejecutable, "
        "no impecable. Un plan correcto con redaccion mejorable se aprueba.\n\n"
        "Responde unicamente con el JSON del esquema."
    ),
    output_schema=VeredictoCritico,
    output_key="veredicto_critico",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.0, max_output_tokens=4096
    ),
    retry_config=REINTENTOS_EN_CICLO,
    timeout=config.TIMEOUT_AGENTE_JUICIO,
)


@node(name="compuerta_critico")
async def compuerta_critico(ctx: Context) -> dict:
    """Decide si el plan vuelve al planificador o avanza. Determinista y ACOTADA.

    Es la unica defensa contra un ciclo infinito en el grafo. Enruta con
    `ctx.route`:
      'reintentar' -> vuelve a agente_plan_accion
      'continuar'  -> avanza a nodo_decision
    """
    iteracion = int(ctx.state.get("iter_critico", 0)) + 1
    ctx.state["iter_critico"] = iteracion

    veredicto = ctx.state.get("veredicto_critico") or {}
    aprobado = bool(veredicto.get("aprobado"))
    cobertura = int(veredicto.get("cobertura_hallazgos", 0))
    puntaje = int(veredicto.get("puntaje_rubrica", 0))

    # --- la verificacion determinista del invariante -------------------------
    # El plan declara `hallazgos_sin_accion`. No le creemos: lo recalculamos.
    # Pedirselo sirve para que se autocorrija antes de responder; recalcularlo
    # sirve para atraparlo cuando no lo hizo.
    plan = ctx.state.get("plan_accion") or {}
    evaluacion = ctx.state.get("evaluacion_riesgo") or {}
    codigos_hallazgo = {h.get("codigo") for h in (evaluacion.get("hallazgos") or [])}
    codigos_accion = {a.get("codigo_hallazgo") for a in (plan.get("acciones") or [])}
    huerfanos = sorted(c for c in codigos_hallazgo - codigos_accion if c)
    sin_respaldo = sorted(c for c in codigos_accion - codigos_hallazgo if c)

    ctx.state["huerfanos_reales"] = huerfanos
    ctx.state["acciones_sin_respaldo_reales"] = sin_respaldo

    agotado = iteracion >= config.MAX_ITER_CRITICO
    cobertura_ok = not huerfanos and cobertura >= config.UMBRAL_COBERTURA_CRITICO

    if agotado or (aprobado and not huerfanos) or cobertura_ok:
        ctx.route = "continuar"
        if agotado and (not aprobado or huerfanos):
            # Se agoto el ciclo sin un plan aprobado. No se bloquea el
            # pipeline: se emite el expediente con la senal, y el eje ops la
            # convierte en una RecomendacionOps('ciclo_critico_agotado').
            ctx.state["senal_ciclo_agotado"] = {
                "iteraciones": iteracion,
                "puntaje_rubrica": puntaje,
                "cobertura": cobertura,
                "hallazgos_sin_accion": huerfanos,
                "instrucciones_pendientes": veredicto.get("instrucciones_de_mejora", ""),
            }
    else:
        ctx.route = "reintentar"
        ctx.state["instrucciones_mejora_critico"] = (
            f"{veredicto.get('instrucciones_de_mejora', '')}\n"
            f"Hallazgos que siguen SIN accion propuesta: "
            f"{', '.join(huerfanos) or 'ninguno'}."
        ).strip()

    return {
        "iteracion": iteracion,
        "ruta": ctx.route,
        "aprobado": aprobado,
        "cobertura_declarada": cobertura,
        "hallazgos_sin_accion_reales": huerfanos,
        "acciones_sin_respaldo_reales": sin_respaldo,
        "ciclo_agotado": agotado,
        "max_iteraciones": config.MAX_ITER_CRITICO,
    }

