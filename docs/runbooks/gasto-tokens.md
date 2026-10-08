# Runbook — Gasto de tokens alto

**Sintoma:** La factura supero `config.PRESUPUESTO_TOKENS_FACTURA`.

## Diagnostico
1. Cada validador reenvia su JSON Schema en cada llamada (~6k tokens entre los 4).
2. Verificar que no hubo vueltas extra del critico (`iteraciones_critico`).

## Accion
1. Degradar el modelo de los validadores.
2. Acortar descripciones en los esquemas de salida.

## Escalamiento
ingenieria_agentes
