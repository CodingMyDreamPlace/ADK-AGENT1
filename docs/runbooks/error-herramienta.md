# Runbook — Error de herramienta

**Sintoma:** Una herramienta determinista fallo (`on_tool_error_callback`). El validador debio responder `no_evaluable`.

## Diagnostico
1. Leer `evidencia` de la recomendacion: nombra el archivo o la tabla.
2. `python -m ap_ops.scripts.smoke_esquemas` — si falla igual, el problema es de datos, no del modelo.
3. Confirmar que `ap_ops/datos/*.json` existen y parsean (`DatoNoDisponible` indica cual).

## Accion
1. Restaurar el maestro faltante o corregir el JSON.
2. Reprocesar la factura: sigue `no_evaluable` hasta que el insumo exista.

## Escalamiento
ingenieria_agentes si el maestro es una consulta a BigQuery/ERP y no un archivo.
