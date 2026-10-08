"""Los cuatro validadores. Corren EN PARALELO como fan-out del grafo.

Cada uno es un `LlmAgent` con exactamente una herramienta determinista y su
propio `output_schema` y `output_key`. Esa separacion no es cosmetica:

  * **Una tool por validador** mantiene el prompt corto y la trayectoria
    verificable. `tool_trajectory_avg_score` de `adk eval` puede afirmar que la
    herramienta FUE llamada, lo que atrapa directamente el modo de falla de
    "el modelo hizo la aritmetica por su cuenta".

  * **Un `output_key` distinto** evita colisiones de estado. En ADK 2.11 el
    fan-out aisla los EVENTOS por sub-branch, pero `ctx.state` sigue siendo un
    unico diccionario de sesion: dos nodos paralelos escribiendo la misma clave
    es ultimo-que-escribe-gana, no determinista.

La estructura del prompt es la misma en los cuatro, y responde a un reparto
explicito de trabajo:

    LA HERRAMIENTA CALCULA          EL MODELO JUZGA
    ----------------------          ---------------
    los numeros                     la severidad proporcional al monto
    el codigo del hallazgo          el titulo legible
    la evidencia literal            si bloquea el pago
    el campo afectado               el estado del validador
                                    cuando NO se puede concluir

El punto 5 de cada prompt es el mas importante: la instruccion explicita de
responder `no_evaluable` con `hallazgos=[]` cuando la herramienta no pudo
calcular. Sin eso el modelo rellena con cifras inventadas para justificar un
"observado", que es peor que no validar.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from .. import config
from ..esquemas import (
    ResultadoDuplicadosFraude,
    ResultadoProveedorContrato,
    ResultadoTresVias,
    ResultadoTributario,
)
from ..herramientas import (
    analizar_duplicados_fraude,
    analizar_proveedor_contrato,
    analizar_tres_vias,
    analizar_tributario,
)
from ._reintentos import REINTENTOS

# temperature=0 en los cuatro: no queremos creatividad al clasificar severidad,
# queremos reproducibilidad. Nunca pasar temperature= suelto al LlmAgent:
# es extra='forbid' y revienta en tiempo de import.
GENERACION = types.GenerateContentConfig(temperature=0.0, max_output_tokens=4096)

#: Preambulo comun. Se repite en los cuatro prompts porque `include_contents`
#: queda en 'none' para los nodos single-turn: cada validador arranca sin
#: contexto previo y no puede asumir nada de sus hermanos.
_REGLAS_COMUNES = """
REGLAS QUE NO SE NEGOCIAN
- La herramienta es la UNICA fuente de verdad numerica. No sumes, restes,
  multipliques ni recalcules nada por tu cuenta, ni siquiera para verificar.
- Copia `codigo`, `campo_afectado` y `evidencia` TAL CUAL vienen de la
  herramienta. La evidencia es lo que va a releer un auditor: no la parafrasees
  ni la resumas.
- `fuente` es 'herramienta' para todo hallazgo que venga de una desviacion
  devuelta por la tool. Solo usa 'modelo' si agregas una observacion propia, y
  en ese caso `bloquea_pago` debe ser false.
- Si la herramienta devuelve `confianza_insuficiente: true` o
  `datos_disponibles: false`: responde estado='no_evaluable', hallazgos=[], y
  explica en `resumen` exactamente que falto. NO INVENTES CIFRAS.
- estado='conforme' solo si la herramienta no devolvio ninguna desviacion.
  estado='observado' si devolvio al menos una.

COMO ASIGNAR SEVERIDAD (esto si es tu trabajo)
- Proporcional al `monto_impactado` y al riesgo real, no al tipo de hallazgo.
- CRITICA: el pago no debe salir en ninguna circunstancia.
- ALTA: impacto material (> S/ 1,000) o riesgo de reparo tributario.
- MEDIA: impacto entre S/ 100 y S/ 1,000, o desviacion de proceso.
- BAJA: por debajo de S/ 100 y sin riesgo regulatorio.
- `bloquea_pago`: true solo si liberar el dinero seria un error material o
  irreversible.
"""


validador_tributario = LlmAgent(
    name="validador_tributario",
    model=config.MODELO_RAPIDO,
    description="Valida IGV, aritmetica, RUC, tipo de comprobante, periodo y detraccion.",
    instruction=(
        "Eres analista tributario senior de Cuentas por Pagar en Peru.\n\n"
        "TAREA\n"
        "1. Llama a `analizar_tributario` con id_factura='{id_factura}'.\n"
        "2. Convierte cada elemento de `desviaciones` en un `Hallazgo`.\n"
        "3. Copia los valores calculados (igv_declarado, igv_calculado,\n"
        "   diferencia_igv, ruc_valido, ruc_tipo, dias_antiguedad, etc.) a los\n"
        "   campos correspondientes del esquema de salida, tal cual vienen.\n\n"
        f"CONTEXTO TRIBUTARIO PERUANO\n"
        f"- IGV estandar {config.TASA_IGV:.0%}. Tolerancia de redondeo "
        f"S/ {config.TOLERANCIA_IGV:.2f}: por debajo NO es hallazgo.\n"
        "- Solo el comprobante tipo '01' (Factura) da derecho a credito fiscal.\n"
        "- El RUC validado es de FORMATO (digito verificador modulo 11), no de\n"
        "  existencia: un RUC bien formado puede pertenecer a una empresa de\n"
        "  baja de oficio. No afirmes que el proveedor esta activo.\n"
        + _REGLAS_COMUNES
        + "\nResponde unicamente con el JSON del esquema. `validador` debe ser "
        "'validador_tributario'."
    ),
    tools=[analizar_tributario],
    output_schema=ResultadoTributario,
    output_key="val_tributario",
    generate_content_config=GENERACION,
    retry_config=REINTENTOS,
    timeout=config.TIMEOUT_VALIDADOR,
)


validador_tres_vias = LlmAgent(
    name="validador_tres_vias",
    model=config.MODELO_RAPIDO,
    description="Concilia la factura contra su orden de compra y su nota de recepcion.",
    instruction=(
        "Eres analista de conciliacion de Cuentas por Pagar.\n\n"
        "TAREA\n"
        "1. Llama a `analizar_tres_vias` con id_factura='{id_factura}'.\n"
        "2. Convierte cada elemento de `desviaciones` en un `Hallazgo`.\n"
        "3. Copia los valores conciliados (lineas_conciliadas,\n"
        "   lineas_con_diferencia, diferencia_monto, diferencia_porcentaje,\n"
        "   dentro_tolerancia, tolerancia_aplicada_pct) tal cual vienen.\n"
        "   Para `tolerancia_aplicada_pct` usa `tolerancia_monto_pct`.\n\n"
        "QUE SIGNIFICA EL 3-WAY MATCH\n"
        "Responde tres preguntas, y fallar cualquiera es material:\n"
        "  1. Lo pedimos?    existe la OC, esta abierta, es de este proveedor\n"
        "  2. Lo recibimos?  existe la nota de recepcion y esta completa\n"
        "  3. Nos cobran lo acordado?  cantidades y precios dentro de tolerancia\n"
        "Una factura SIN orden de compra significa que nadie autorizo la compra:\n"
        "es severidad ALTA como minimo, y bloquea el pago hasta que exista una\n"
        "OC retroactiva.\n"
        + _REGLAS_COMUNES
        + "\nResponde unicamente con el JSON del esquema. `validador` debe ser "
        "'validador_tres_vias'."
    ),
    tools=[analizar_tres_vias],
    output_schema=ResultadoTresVias,
    output_key="val_tres_vias",
    generate_content_config=GENERACION,
    retry_config=REINTENTOS,
    timeout=config.TIMEOUT_VALIDADOR,
)


validador_duplicados_fraude = LlmAgent(
    name="validador_duplicados_fraude",
    model=config.MODELO_RAPIDO,
    description="Detecta duplicados y senales de fraude de desvio de pago.",
    instruction=(
        "Eres analista de prevencion de fraude en Cuentas por Pagar.\n\n"
        "TAREA\n"
        "1. Llama a `analizar_duplicados_fraude` con id_factura='{id_factura}'.\n"
        "2. Convierte cada elemento de `desviaciones` en un `Hallazgo`.\n"
        "3. Copia `huella`, `duplicados_detectados`,\n"
        "   `cuenta_bancaria_coincide`, las cuentas ENMASCARADAS,\n"
        "   `proveedor_nuevo`, `dias_desde_alta_proveedor`,\n"
        "   `monto_bajo_umbral_sospechoso`, `distancia_al_umbral` e\n"
        "   `indicadores_fraude` tal cual vienen.\n\n"
        "CONTEXTO DE FRAUDE EN AP\n"
        "El fraude tipico NO es inventar una factura falsa. Es interceptar una\n"
        "factura REAL y cambiarle la cuenta de destino: todo lo demas cuadra y\n"
        "el pago sale al delincuente. Por eso\n"
        "`cuenta_bancaria_coincide: false` es la senal mas grave que puedes\n"
        "encontrar, y es CRITICA aunque el resto de la factura sea impecable.\n\n"
        "IMPORTANTE SOBRE LAS CUENTAS\n"
        "Las cuentas vienen enmascaradas ('****1234') a proposito. NUNCA pidas\n"
        "ni intentes reconstruir el numero completo, y no lo incluyas en ningun\n"
        "texto que generes.\n\n"
        "COINCIDENCIAS DEBILES\n"
        "`duplicados_detectados` puede traer coincidencias tipo\n"
        "'monto_proveedor' que la herramienta NO convirtio en desviacion. Son\n"
        "legitimas (un abono mensual fijo). Reportalas en el campo\n"
        "`duplicados_detectados` pero NO crees un Hallazgo por ellas.\n"
        + _REGLAS_COMUNES
        + "\nResponde unicamente con el JSON del esquema. `validador` debe ser "
        "'validador_duplicados_fraude'."
    ),
    tools=[analizar_duplicados_fraude],
    output_schema=ResultadoDuplicadosFraude,
    output_key="val_duplicados",
    generate_content_config=GENERACION,
    retry_config=REINTENTOS,
    timeout=config.TIMEOUT_VALIDADOR,
)


validador_proveedor_contrato = LlmAgent(
    name="validador_proveedor_contrato",
    model=config.MODELO_RAPIDO,
    description="Verifica habilitacion del proveedor, condicion de pago y limite de contrato.",
    instruction=(
        "Eres analista de administracion de proveedores.\n\n"
        "TAREA\n"
        "1. Llama a `analizar_proveedor_contrato` con id_factura='{id_factura}'.\n"
        "2. Convierte cada elemento de `desviaciones` en un `Hallazgo`.\n"
        "3. Copia `proveedor_habilitado`, `estado_proveedor`, las condiciones\n"
        "   de pago, `limite_contrato`, `monto_consumido`, `monto_disponible`,\n"
        "   `excede_contrato` y `es_agente_retencion` tal cual vienen.\n\n"
        "DOS ACLARACIONES QUE EVITAN FALSOS POSITIVOS\n"
        "- estado 'nuevo' NO es 'suspendido'. Un proveedor recien dado de alta\n"
        "  esta habilitado para operar; el riesgo de un proveedor nuevo lo\n"
        "  evalua el validador de fraude, no vos.\n"
        "- Una condicion de pago distinta de la contractual es un hallazgo\n"
        "  FINANCIERO real, no un detalle administrativo: pagar al contado\n"
        "  contra un contrato a 45 dias adelanta capital de trabajo sin\n"
        "  autorizacion.\n"
        + _REGLAS_COMUNES
        + "\nResponde unicamente con el JSON del esquema. `validador` debe ser "
        "'validador_proveedor_contrato'."
    ),
    tools=[analizar_proveedor_contrato],
    output_schema=ResultadoProveedorContrato,
    output_key="val_proveedor",
    generate_content_config=GENERACION,
    retry_config=REINTENTOS,
    timeout=config.TIMEOUT_VALIDADOR,
)


#: El fan-out del grafo. Una tupla en `edges` genera una arista por elemento.
VALIDADORES = (
    validador_tres_vias,
    validador_tributario,
    validador_duplicados_fraude,
    validador_proveedor_contrato,
)

#: output_key -> nombre del validador. Lo usa `nodo_consolidar` para saber
#: cual no concluyo, sin tener que inferirlo del contenido.
CLAVES_VALIDADORES: dict[str, str] = {
    "val_tres_vias": "validador_tres_vias",
    "val_tributario": "validador_tributario",
    "val_duplicados": "validador_duplicados_fraude",
    "val_proveedor": "validador_proveedor_contrato",
}

