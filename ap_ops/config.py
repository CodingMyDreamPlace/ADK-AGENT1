"""Parametros del sistema, en un solo lugar.

Todo umbral, tolerancia y tasa vive aca. Ninguna herramienta ni ningun prompt
tiene numeros magicos escritos adentro, por dos razones:

  1. `DecisionAP.umbral_usado` tiene que poder reportar el valor concreto que se
     comparo. Si el umbral esta hardcodeado en tres lugares, el expediente deja
     de ser reproducible cuando uno de los tres cambia.
  2. Los prompts interpolan estos valores (p.ej. la tolerancia de IGV) para que
     el modelo y la herramienta usen exactamente el mismo numero. Si divergen,
     el modelo reporta hallazgos que la herramienta no calculo.
"""

from __future__ import annotations

import os

VERSION_PIPELINE = "0.1.0"

# ---------------------------------------------------------------- cuota
#: Perfil de cuota del proveedor. 'free' | 'pago'
#:
#: ESTO NO ES UN DETALLE DE CONFIGURACION, ES UNA RESTRICCION ARQUITECTONICA.
#:
#: El free tier de AI Studio permite 5 requests por minuto por modelo. El
#: fan-out de 4 validadores son 8 requests en rafaga, porque cada validador
#: hace DOS llamadas: la que emite el function call y la que devuelve el JSON
#: estructurado despues de que la herramienta respondio. Ocho requests contra
#: un limite de cinco es un 429 garantizado.
#:
#: En perfil 'free' el pipeline sigue funcionando pero en serie y con esperas
#: largas: una factura tarda minutos en lugar de segundos. Sirve para
#: desarrollo y para los golden tests, no para procesar un lote.
#:
#: El perfil 'pago' requiere facturacion habilitada en AI Studio, o migrar a
#: Vertex (GOOGLE_GENAI_USE_ENTERPRISE=1 + ADC), que es lo que pide la etapa B
#: del camino de migracion en ARQUITECTURA.md.
PERFIL_CUOTA = os.environ.get("AP_OPS_PERFIL_CUOTA", "free").lower()
ES_FREE_TIER = PERFIL_CUOTA == "free"

#: Nodos del fan-out que corren a la vez. En free tier: 1 (serie).
MAX_CONCURRENCIA = 1 if ES_FREE_TIER else 4

#: Reintentos de los validadores ante 429. En free tier el delay tiene que
#: superar la ventana de un minuto del limite de RPM, o los tres intentos se
#: consumen dentro de la misma ventana agotada.
REINTENTO_MAX_INTENTOS = 5 if ES_FREE_TIER else 3
REINTENTO_DELAY_INICIAL = 30.0 if ES_FREE_TIER else 1.0
REINTENTO_DELAY_MAXIMO = 70.0 if ES_FREE_TIER else 30.0

#: Timeouts. En free tier hay que dar espacio a las esperas de cuota.
TIMEOUT_VALIDADOR = 300.0 if ES_FREE_TIER else 90.0
TIMEOUT_AGENTE_JUICIO = 300.0 if ES_FREE_TIER else 150.0
TIMEOUT_PIPELINE = 1800.0 if ES_FREE_TIER else 300.0

# ---------------------------------------------------------------- modelos
#: Validadores y critico: tarea acotada, salida estructurada, temperatura 0.
#:
#: Por que flash-LITE y no flash: la cuota del free tier es POR MODELO
#: (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, 20 requests/dia). Con
#: los validadores y el critico en un modelo distinto del que usan scoring,
#: plan y coordinador, el pipeline tiene dos buckets diarios en lugar de uno.
#:
#: El cambio es defendible por diseno, no solo por cuota: los validadores NO
#: calculan nada. La herramienta ya hizo la aritmetica, los lookups y las
#: comparaciones; el modelo solo traduce `desviaciones` a `Hallazgo` tipados y
#: asigna severidad proporcional. Para eso alcanza un modelo mas chico. El
#: juicio que si necesita capacidad —correlacionar tres senales debiles en un
#: fraude, redactar un plan ejecutable— vive en MODELO_JUICIO.
MODELO_RAPIDO = "gemini-3.5-flash-lite"

#: Scoring, plan de accion y coordinador: requieren correlacion y redaccion.
MODELO_JUICIO = "gemini-3.8-flash"

# ---------------------------------------------------------------- tributario
TASA_IGV = 0.18
#: Tolerancia de redondeo en soles. Por debajo de esto NO es hallazgo.
#: Existe porque el proveedor puede redondear por linea y nosotros por total.
TOLERANCIA_IGV = 0.05
#: Tolerancia para la aritmetica general (subtotal + igv + otros == total).
TOLERANCIA_ARITMETICA = 0.05
#: Una factura mas vieja que esto ya no es deducible.
DIAS_ANTIGUEDAD_MAX = 365

#: Confianza minima del OCR para que valga la pena validar.
#: Por debajo de esto las herramientas devuelven `confianza_insuficiente=True` y
#: los validadores deben responder estado='no_evaluable' con hallazgos=[].
#: Validar cifras mal leidas es peor que no validar: produce hallazgos falsos
#: que el analista tiene que descartar a mano, y erosiona la confianza mas
#: rapido que un falso negativo.
CONFIANZA_OCR_MINIMA = 0.70

#: Detraccion (SPOT): se aplica a ciertos servicios por encima de este monto.
#: Tabla SIMPLIFICADA del Anexo 3 de SUNAT, suficiente para el demo. En
#: produccion esto sale de una tabla parametrica, no de constantes.
DETRACCION_MONTO_MINIMO = 700.0
TASAS_DETRACCION: dict[str, float] = {
    "012": 0.12,  # intermediacion laboral y tercerizacion
    "019": 0.12,  # arrendamiento de bienes
    "022": 0.12,  # otros servicios empresariales
    "027": 0.04,  # servicio de transporte de carga
    "037": 0.12,  # demas servicios gravados
}

# ---------------------------------------------------------------- 3-way match
#: Tolerancias porcentuales por linea. Diferencias por debajo no son hallazgo:
#: un 1% de merma en una entrega de granel es normal, no una desviacion.
TOLERANCIA_CANTIDAD_PCT = 2.0
TOLERANCIA_PRECIO_PCT = 2.0
#: Tolerancia sobre el monto total de la factura vs la OC.
TOLERANCIA_MONTO_PCT = 2.0

# ---------------------------------------------------------------- fraude
#: Un proveedor dado de alta hace menos de esto es "nuevo". Monto alto de un
#: proveedor nuevo es una de las tres patas del patron de fraude clasico.
DIAS_PROVEEDOR_NUEVO = 90
#: Umbral de aprobacion de la organizacion: por encima requiere firma de jefatura.
UMBRAL_APROBACION = 10_000.0
#: Que tan cerca del umbral (por debajo) se considera fraccionamiento deliberado.
MARGEN_SPLIT_PCT = 5.0
#: Monto por encima del cual un proveedor nuevo ya es señal por si solo.
MONTO_ALTO_PROVEEDOR_NUEVO = 5_000.0

# ---------------------------------------------------------------- decision
#: Puntaje de riesgo por debajo del cual la factura puede ir touchless.
UMBRAL_TOUCHLESS = 25
#: Puntaje por encima del cual se retiene.
UMBRAL_RETENER = 60
#: Monto por encima del cual SIEMPRE hay confirmacion humana, sin importar
#: el puntaje. Una factura limpia de S/ 80.000 igual la mira alguien.
UMBRAL_MONTO_HITL = 10_000.0

#: Pesos del puntaje determinista por severidad. La suma se trunca a 100.
PESO_SEVERIDAD: dict[str, int] = {
    "informativa": 0,
    "baja": 5,
    "media": 15,
    "alta": 30,
    "critica": 50,
}
#: Penalizacion fija por cada validador que no pudo concluir. No saber es riesgo.
PESO_NO_EVALUABLE = 10

# ---------------------------------------------------------------- ciclo critico
#: Tope de vueltas del ciclo plan -> critico -> plan. ADK 2.11 NO tiene cap de
#: iteraciones en el grafo: este numero es la unica defensa contra gasto infinito.
MAX_ITER_CRITICO = 2
#: Si el critico puntua la cobertura por encima de esto, se acepta el plan.
UMBRAL_COBERTURA_CRITICO = 90

# ---------------------------------------------------------------- presupuestos
#: Presupuesto de latencia por agente, en segundos. Excederlo no aborta nada:
#: emite una SenalOps('latencia_alta') con su RecomendacionOps.
PRESUPUESTO_LATENCIA_S: dict[str, float] = {
    "validador_tres_vias": 20.0,
    "validador_tributario": 20.0,
    "validador_duplicados_fraude": 20.0,
    "validador_proveedor_contrato": 20.0,
    "agente_scoring_riesgo": 30.0,
    "agente_plan_accion": 40.0,
    "agente_critico": 25.0,
    "coordinador_ap_ops": 30.0,
}
#: Techo de tokens por factura. Excederlo emite SenalOps('gasto_tokens_alto').
PRESUPUESTO_TOKENS_FACTURA = 60_000

# ---------------------------------------------------------------- flags
#: Si es True, las senales ops de tipo 'desconocida' van a un LlmAgent en vez
#: de la tabla de reglas. Apagado por default: el eje ops esta en el camino
#: caliente y tiene que seguir funcionando cuando hay tormenta de errores.
OPS_ADVISOR_LLM = False
