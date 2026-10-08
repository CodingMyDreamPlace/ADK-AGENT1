# Runbook — Tormenta de reintentos

**Sintoma:** Un mismo nodo supero 2 reintentos.

## Diagnostico
1. Distinguir causa: cuota (429), saturacion (503) o dato corrupto (se repite siempre).
2. Revisar `AP_OPS_PERFIL_CUOTA`: en `free` el limite es 5 req/min y 20 req/dia POR MODELO.

## Accion
1. Si es cuota diaria: reintentar no sirve. Cambiar de modelo o habilitar facturacion.
2. Si es dato corrupto: corregir la fuente; no subir `max_attempts`.

## Escalamiento
plataforma
