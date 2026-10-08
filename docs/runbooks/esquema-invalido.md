# Runbook — Esquema invalido

**Sintoma:** La salida de un agente no valido contra su `output_schema`.

## Diagnostico
1. Comparar la salida cruda con el esquema: campo faltante, enum fuera de catalogo, tipo incorrecto.

## Accion
1. Corregir el prompt o el esquema. Los esquemas no admiten Decimal, datetime ni dict[str, Any].

## Escalamiento
ingenieria_agentes
