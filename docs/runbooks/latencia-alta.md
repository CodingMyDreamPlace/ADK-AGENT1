# Runbook — Latencia sobre presupuesto

**Sintoma:** Una llamada al modelo excedio `config.PRESUPUESTO_LATENCIA_S`.

## Diagnostico
1. Comparar `latencias_por_nodo` del `RegistroAuditoria` contra el presupuesto del agente.

## Accion
1. Si es puntual: ignorar.
2. Si es recurrente: subir el timeout del nodo o pasar a un modelo mas rapido.

## Escalamiento
ingenieria_agentes
