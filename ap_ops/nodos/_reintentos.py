"""Politicas de reintento. Dos, y la diferencia importa.

El problema que resuelven son dos fallas distintas que se ven parecidas:

  429 RESOURCE_EXHAUSTED  cuota agotada. En el free tier de AI Studio son 5
                          requests/minuto y 20/dia POR MODELO. El retry sirve
                          para el limite por minuto; contra el diario no hay
                          backoff que alcance.

  503 UNAVAILABLE         el modelo esta sobrecargado. Es pasajero y de
                          segundos. Reintentar SIEMPRE conviene.

Y el riesgo de reintentar de mas: `retry_config` se MULTIPLICA dentro de un
ciclo. Un `max_attempts=5` en un nodo que esta en el ciclo del critico con
`MAX_ITER_CRITICO=2` son hasta 10 llamadas al modelo por factura. De ahi las
dos politicas:

  REINTENTOS          nodos FUERA del ciclo (los 4 validadores, scoring).
                      Generoso, porque el peor caso esta acotado por el nodo.

  REINTENTOS_EN_CICLO nodos DENTRO del ciclo (plan de accion, critico).
                      max_attempts=2 y solo ante excepciones transitorias: un
                      503 no debe tumbar el pipeline, pero un error de logica
                      tampoco debe reintentarse dos veces por vuelta.
"""

from __future__ import annotations

from google.adk.workflow import RetryConfig

from .. import config

#: Nombres de clase de las excepciones que vale la pena reintentar.
#: `RetryConfig.exceptions` filtra por nombre de clase
#: (`_retry_config.py:60-77`), asi que no hace falta importar las clases
#: privadas de ADK ni de google-genai.
#:
#:   ServerError              google.genai.errors  -> 503, 500
#:   _ResourceExhaustedError  google.adk.models.google_llm -> 429
#:   ClientError              google.genai.errors  -> 4xx (incluye el 429 crudo)
TRANSITORIAS: list[str] = [
    "ServerError",
    "_ResourceExhaustedError",
    "ClientError",
    "ServiceUnavailable",
    "DeadlineExceeded",
    "TimeoutError",
]

#: Para nodos fuera del ciclo. Los delays salen del perfil de cuota: en free
#: tier el delay inicial tiene que superar la ventana de un minuto del limite
#: de RPM, o los reintentos se consumen dentro de la misma ventana agotada.
REINTENTOS = RetryConfig(
    max_attempts=config.REINTENTO_MAX_INTENTOS,
    initial_delay=config.REINTENTO_DELAY_INICIAL,
    max_delay=config.REINTENTO_DELAY_MAXIMO,
    backoff_factor=2.0,
    jitter=0.5,
)

#: Para nodos dentro del ciclo del critico. Acotado a 2 intentos y solo ante
#: excepciones transitorias: el peor caso son 4 llamadas por agente y por
#: factura (2 intentos x MAX_ITER_CRITICO=2), no 10.
REINTENTOS_EN_CICLO = RetryConfig(
    max_attempts=2,
    initial_delay=config.REINTENTO_DELAY_INICIAL,
    max_delay=config.REINTENTO_DELAY_MAXIMO,
    backoff_factor=2.0,
    jitter=0.5,
    exceptions=TRANSITORIAS,
)
