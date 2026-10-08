# Evaluacion de AP Ops

Dos capas, de la mas barata a la mas cara. Correr siempre la primera antes de gastar cuota en la segunda.

## Capa 1 — gratis (pytest, 0 llamadas al modelo)

```powershell
.\.venv\Scripts\python.exe -m pytest ap_ops/tests -q
```

| Archivo | Que protege |
|---|---|
| `test_expedientes_dorados.py` | Las 7 facturas sinteticas: codigos, decision y regla exactos. |
| `test_evaluacion.py` | Un **expediente real generado por Gemini**, congelado en `tests/dorados/`. Verifica el contrato modelo <-> codigo determinista. |
| `test_grafo.py` | Estructura del grafo; cada `output_schema` es aceptado por `types.Schema` de Gemini. |

El golden file se regenera con una corrida real:

```powershell
.\.venv\Scripts\python.exe -m ap_ops.scripts.correr_pipeline --fixture factura_fraude_cuenta --json
Copy-Item salidas\factura_fraude_cuenta.expediente.json ap_ops\tests\dorados\
```

## Capa 2 — `adk eval` (GASTA CUOTA)

Ejecuta el coordinador de verdad, incluido el puente `run_node` hacia el pipeline.

| Caso | Costo aprox. |
|---|---|
| `triar_factura_limpia` | 10 inferencias, ~31.000 tokens, ~50 s (medido) |
| `triar_factura_con_fraude` | ~13 inferencias |

```powershell
# un caso
adk eval ap_ops "ap_ops\eval\triaje_basico.evalset.json:triar_factura_limpia" `
  --config_file_path ap_ops\eval\test_config.json --print_detailed_results

# los dos
adk eval ap_ops ap_ops\eval\triaje_basico.evalset.json --config_file_path ap_ops\eval\test_config.json
```

El veredicto no sale en la tabla detallada: queda en
`ap_ops/.adk/eval_history/*.evalset_result.json` (`finalEvalStatus: 1` = PASSED).

### Configs

- `test_config.json` — solo `tool_trajectory_avg_score` (IN_ORDER). **No llama a un modelo juez.**
- `test_config_juez.json` — agrega `rubric_based_final_response_quality_v1` con 4 rubricas (cita codigos, acciones con responsable, no afirma pago, cuenta enmascarada). El juez agrega llamadas por caso.

### Por que `IN_ORDER` y no `EXACT`

El eval registra tambien las tools internas de los sub-agentes (`analizar_*`, `set_model_response`).
`EXACT` fallaria por llamadas extra que son correctas. `IN_ORDER` exige que `triar_factura` ocurra y tolera el resto.

## Limites del free tier de AI Studio

20 requests/dia **por modelo** y 5/minuto. Los validadores y el critico usan `gemini-3.5-flash-lite`; scoring, plan y coordinador usan `gemini-3.8-flash`. Una corrida de eval gasta ~9 del bucket de flash-lite: dos casos al dia como maximo.
