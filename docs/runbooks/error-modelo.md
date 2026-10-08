# Runbook — Error del modelo

**Sintoma:** La llamada al modelo fallo (`on_model_error_callback`).

## Diagnostico
1. 429 RESOURCE_EXHAUSTED: cuota. Ver `GenerateRequestsPerDayPerProjectPerModel` vs `PerMinute`.
2. 503 UNAVAILABLE: saturacion del proveedor, transitoria.

## Accion
1. 503: el `retry_config` del nodo deberia absorberlo; si persiste minutos, cambiar `MODELO_JUICIO`.
2. 429 diario: no hay backoff que alcance. Esperar la reposicion, cambiar de modelo o migrar a Vertex.

## Escalamiento
plataforma
