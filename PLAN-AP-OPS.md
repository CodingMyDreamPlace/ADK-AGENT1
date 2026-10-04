# AP Ops — Orquestación multiagente para triaje de Cuentas por Pagar (ADK 2.11)

## Contexto

Hoy el repo es un scaffold de un solo agente (`agent_1/`): un `LlmAgent` con una tool mock
(`get_current_time`) que devuelve `"10:30 AM"` fijo. No hay git, ni `requirements.txt`, ni
estructura para crecer. El objetivo es **escalar de "un agente con una tool" a un sistema
multiagente orquestado** sobre un caso de negocio empresarial real.

**Caso elegido: triaje autónomo de excepciones de facturas en Cuentas por Pagar (AP).**
Es el proceso donde toda empresa pierde dinero y tiempo: un analista abre cada factura, la cruza
contra la orden de compra y la recepción, revisa IGV y RUC, busca duplicados, verifica que la
cuenta bancaria sea la registrada, y recién entonces decide pagar o retener. El KPI de la industria
es **% de facturas *touchless*** (procesadas sin intervención humana).

Se eligió porque ejercita **todos** los patrones de orquestación a la vez — fan-out paralelo,
join, ciclo acotado con agente crítico, compuerta determinista, human-in-the-loop — y corre
100% con datos sintéticos, sin credenciales ni integraciones externas.

**Resultado esperado:** dada una factura, el sistema emite un `ExpedienteAP` con hallazgos
tipados, un plan de acciones concretas (responsable + SLA + evidencia), una decisión auditable
(`touchless_approve` / `aprobar_con_condiciones` / `retener` / `rechazar`) y un memo de auditoría.
Nada que mueva dinero se ejecuta sin confirmación humana.

**Requisito transversal pedido explícitamente: "ante una observación, proponer acciones".**
Se implementa en dos ejes:
- **Eje negocio** — todo `Hallazgo` produce obligatoriamente ≥1 `AccionPropuesta` vinculada por
  `codigo_hallazgo`. Invariante verificable: `plan_accion.hallazgos_sin_accion == []`.
- **Eje operaciones** — toda señal de observabilidad (error de tool, tormenta de reintentos,
  latencia/tokens sobre presupuesto, desacuerdo LLM-vs-determinista, baja confianza) produce una
  `RecomendacionOps` accionable con runbook, no solo una línea de log.

---

## Decisión de estructura (respuesta a tu pregunta)

Preguntaste cuándo conviene cada layout. El criterio real:

| Layout | Cuándo conviene | Por qué |
|---|---|---|
| **Paquetes hermanos** (`agent_1/`, `ap_ops/`) | Cuando cada agente es un producto independiente y querés verlos todos en el dropdown de `adk web`. **Tu caso.** | Es exactamente el layout que `adk web` espera por defecto: `agents_dir/<pkg>/agent.py`. Cero configuración. Y conservás `agent_1` como referencia mínima funcionando. |
| **Reescribir `agent_1` en sitio** | Cuando el scaffold fue un accidente y no aporta nada. | Perdés el ejemplo mínimo que sirve para aislar problemas ("¿falla ADK o falla mi código?"). No vale la pena. |
| **`src/<pkg>/` con `pyproject.toml`** | Cuando el agente se publica como librería, o un equipo con CI/CD lo instala como dependencia. | Rompe el descubrimiento por defecto de ADK: hay que apuntar `adk web src/` y manejar instalación editable. Overhead que hoy no te compra nada. |

**Decisión: paquetes hermanos.** `agent_1/` queda intacto. Si en 6 meses esto se vuelve una
librería instalable, migrar a `src/` es un `git mv` — no una reescritura.

---

## Arquitectura: híbrido `Workflow` + coordinador LLM

ADK 2.11 está en migración: `SequentialAgent` / `ParallelAgent` / `LoopAgent` están **deprecados**
a favor de `Workflow` (grafo de nodos). Pero `Workflow` **no** es un `BaseAgent`, así que no se
puede poner en `LlmAgent.sub_agents`.

**Veredicto verificado en el código fuente: el puente es `await tool_context.run_node(workflow, node_input)`.**
`agents/context.py:432` acepta `NodeLike = BaseNode | BaseTool | Callable`, y `Workflow` **es** un
`BaseNode` (`workflow/_workflow.py:134`). Los 4 riesgos que podían romperlo están verificados como
despejados: el guard de `rerun_on_resume` pasa (`agents/context.py:214`), el scheduler standalone
está explícitamente soportado fuera de un grafo (`workflow/_dynamic_node_scheduler.py:704-718`),
los eventos hijos sí llegan al stream del Runner y a la sesión (`workflow/_node_runner.py:378-393`),
y un coordinador sin `sub_agents` mantiene `use_scheduler=False` (`workflow/utils/_node_runner_utils.py:229`).

**Única limitación real:** un interrupt de HITL *dentro* del workflow no compone de vuelta a través
de un `FunctionTool` — `NodeInterruptedError` escapa como error de tool
(`workflow/_dynamic_node_scheduler.py:745-747`). Se resuelve por diseño, no peleando:

```
ap_ops/            root_agent = coordinador (LlmAgent, chat)      ← superficie conversacional
                     ├─ FunctionTool triar_factura ──run_node──► pipeline_ap (Workflow)
                     ├─ FunctionTool explicar_decision            (lee state, 1 llamada, no 7)
                     ├─ FunctionTool replantear_plan ──run_node──► subflujo_replanteo
                     └─ FunctionTool(autorizar_pago, require_confirmation=...)  ← HITL vive AQUÍ

ap_ops_pipeline/   app = App(root_agent=pipeline_ap, resumability_config=...)
                                                      ← batch / eval / HITL en grafo
```

- El **workflow nunca interrumpe**: es un pipeline de análisis puro que siempre termina en un
  `ExpedienteAP` con una *recomendación*.
- Lo irreversible (liberar dinero) es una tool **del coordinador** con `require_confirmation`.
  Esto coincide con la regla de negocio y esquiva la limitación en vez de combatirla.
- El segundo entry point da determinismo para scripts, eval y batch.

**Alternativas descartadas:** (a) `Workflow` como `App.root_agent` con un `LlmAgent` en `mode='chat'`
como primer nodo — es legal, pero cada turno conversacional re-entra al grafo, así que *"¿por qué
retuviste la factura X?"* re-ejecutaría los 4 validadores; (b) un `FunctionTool` que levanta su
propio `Runner` — los eventos hijos nunca llegan a la sesión ni a la traza del padre.

### El grafo

```
START → nodo_intake ─┬→ validador_tres_vias ────────┐
                     ├→ validador_tributario ───────┤
                     ├→ validador_duplicados_fraude ├→ join_validadores → nodo_consolidar
                     └→ validador_proveedor_contrato┘                          │
                                                                               │
          ┌────────── route="sin_hallazgos" (atajo touchless: −3 llamadas) ─────┤
          │                                                                     │
          │                                        route="con_hallazgos" ───────┘
          │                                                 ↓
          │                                       agente_scoring_riesgo
          │                                                 ↓
          │                                       agente_plan_accion ←──────┐
          │                                                 ↓               │
          │                                        agente_critico           │ route="reintentar"
          │                                                 ↓               │ (ciclo acotado)
          │                                       compuerta_critico ────────┘
          │                                                 │ route="continuar"
          └──────────────────► nodo_decision ◄──────────────┘
                                     ↓
                              nodo_expediente  (único nodo terminal → output_schema=ExpedienteAP)
```

---

## Layout de archivos

```
adk-agent1/                            # git init aquí
├─ .gitignore                          # NUEVO en raíz (agent_1/.gitignore no cubre __pycache__)
├─ .env.example                         # committeado, sin secretos
├─ requirements.txt
├─ README.md
├─ agent_1/                            # ★ INTACTO
│
├─ ap_ops/                             # entry point 1: coordinador conversacional
│  ├─ __init__.py                       # from . import agent
│  ├─ agent.py                          # app = App(name='ap_ops', root_agent=coordinador, plugins=[...])
│  ├─ .env                              # copia de agent_1/.env (ADK carga .env por paquete)
│  ├─ config.py                         # modelos, umbrales, presupuestos, flags
│  ├─ coordinador.py                    # ★ LlmAgent chat + puente run_node + tool HITL
│  ├─ flujo.py                          # ★ pipeline_ap = Workflow(...), subflujo_replanteo
│  ├─ esquemas/                         # comunes, factura, hallazgos, validadores,
│  │                                    #   acciones, expediente, ops
│  ├─ datos/                            # 7 facturas + 4 maestros (seam → BigQuery)
│  ├─ herramientas/                     # ★ 100% determinista, 0 LLM
│  │                                    #   repositorio, tributario, conciliacion, duplicados,
│  │                                    #   proveedor, scoring, decision, memo
│  ├─ nodos/                            # intake, validadores, scoring, plan_accion, cierre
│  ├─ plugins/                          # auditoria.py (BasePlugin), ops_advisor.py (tabla de reglas)
│  ├─ observabilidad/otel.py            # configurar_otel(): consola | otlp | cloud
│  ├─ eval/                             # test_config.json, *.test.json, *.evalset.json
│  ├─ tests/                            # pytest: herramientas + grafo + expedientes dorados
│  └─ scripts/                          # smoke_esquemas, correr_pipeline, resumir_hitl
│
└─ ap_ops_pipeline/                    # entry point 2: Workflow puro (batch / HITL en grafo)
   ├─ __init__.py
   └─ agent.py                          # app = App(root_agent=pipeline_ap, resumability_config=...)
```

`adk web` desde la raíz lista `agent_1`, `ap_ops`, `ap_ops_pipeline` en el dropdown.

---

## Esquemas (campos en español)

**Regla dura para todo lo que sea `output_schema`:** `float` no `Decimal`, `str` ISO no `date`,
nada de `dict[str, Any]`, sin recursión. `Decimal` vive solo dentro de `herramientas/` con
`ROUND_HALF_UP` a 2 decimales, y se convierte en la frontera.

- `comunes.py` — `Severidad` (informativa→critica), `Moneda`, `TipoComprobante` (01/07/08),
  `Categoria`, `DatoVerificado(nombre, valor, fuente)` ← reemplaza `dict[str, Any]`.
- `factura.py` — `LineaFactura`, `CuentaBancaria(banco, tipo_cuenta, numero_cuenta, cci, titular)`,
  `Factura`, `OrdenCompra`, `NotaRecepcion`, `ProveedorMaestro`, `ContextoValidacion`.
- `hallazgos.py` — `Hallazgo(codigo, titulo, severidad, categoria, campo_afectado, valor_declarado,
  valor_esperado, evidencia, fuente: 'herramienta'|'modelo', confianza, monto_impactado, bloquea_pago)`,
  `ConsolidadoValidacion`, `EvaluacionRiesgo(..., puntaje_determinista, desacuerdo_con_determinista)`.
- `validadores.py` — `ResultadoValidadorBase(validador, estado: conforme|observado|no_evaluable,
  hallazgos, resumen, datos_verificados)` + 4 especializaciones + `ResultadosValidacion`.
- `acciones.py` — `AccionPropuesta(id_accion, **codigo_hallazgo**, accion, tipo_accion, responsable,
  prioridad, sla_horas, justificacion, evidencia, impacto_si_no_se_actua, reversible,
  requiere_aprobacion_humana, bloquea_pago)`, `PlanAccion(..., hallazgos_sin_accion,
  acciones_sin_hallazgo)`, `VeredictoCritico(aprobado, puntaje_rubrica, cobertura_hallazgos, ...)`.
- `expediente.py` — `DecisionAP(resultado, motivo, condiciones, regla_aplicada, umbral_usado,
  requiere_confirmacion_humana)`, `ExpedienteAP` (lo agrega todo + `llamadas_llm`, `tokens_consumidos`).
- `ops.py` — `SenalOps(tipo, severidad, componente, detalle, metricas)`,
  `RecomendacionOps(senal, accion_recomendada, tipo_accion, responsable, urgencia, runbook)`,
  `RegistroAuditoria(..., recomendaciones_ops)`.

`codigo_hallazgo` en `AccionPropuesta` es el vínculo que hace verificable el invariante
"ante una observación, una acción".

---

## Fases

### Fase 0 — Higiene de repo
1. `git init`; `.gitignore` en raíz: `__pycache__/`, `*.py[cod]`, `.env`, `.env.*`, `!.env.example`,
   `.adk/`, `*.db`, `*.sqlite*`, `.venv/`, `.pytest_cache/`, `salidas/`, `*.log`.
2. `requirements.txt`: `google-adk[db,eval,gcp,otel-gcp]==2.11.0`, `pydantic>=2.12,<3`, `pytest>=8`,
   `pytest-asyncio>=0.24`. **Los 4 extras no están instalados hoy** — `db` da `DatabaseSessionService`,
   `eval` da `AgentEvaluator`, `gcp` da los exporters de Cloud Trace, `otel-gcp` la instrumentación.
3. `.env.example` + copiar `agent_1/.env` → `ap_ops/.env`.
4. Esqueletos de paquete (`__init__.py` en todos).
5. Commit base.

### Fase 1 — Esquemas + herramientas deterministas + fixtures (0 LLM, 0 costo)
1. `esquemas/*.py` completos.
2. **7 fixtures**, cada una diseñada para disparar exactamente un validador:
   `factura_conforme` (ruta touchless), `factura_igv_erroneo`, `factura_sin_oc`,
   `factura_duplicada`, `factura_fraude_cuenta` (CCI distinto al maestro),
   `factura_split_umbral` (S/ 9,950 contra umbral de S/ 10,000), `factura_ocr_sucio`
   (fuerza `no_evaluable`). Más 4 maestros mutuamente consistentes.
3. `herramientas/repositorio.py` — `cargar_factura`, `buscar_oc`, `buscar_nr`, `buscar_proveedor`,
   `buscar_historial`. **Este módulo es el único seam a BigQuery**: cambiar los `json.load` por
   `BigQueryToolset.execute_sql` de `google.adk.integrations.bigquery` con
   `BigQueryToolConfig(write_mode=WriteMode.BLOCKED)` y nada encima se toca.
4. Las 4 `analizar_*`. Devuelven **hechos calculados**, nunca juicios de severidad.
   RUC mod-11: pesos `[5,4,3,2,7,6,5,4,3,2]` sobre los 10 primeros dígitos, `r = 11 - (sum % 11)`,
   `10→0`, `11→1`. IGV se compara con tolerancia ±S/0.05, **nunca `==`**.
5. `scoring.py` (`puntaje_determinista`), `decision.py` (`aplicar_compuerta` con tabla numerada
   `R-01..R-08` para que `DecisionAP.regla_aplicada` sea auditable), `memo.py`.
6. `tests/test_*.py` + `scripts/smoke_esquemas.py`.

### Fase 2 — Agentes + grafo + coordinador
1. `config.py` — `MODELO_RAPIDO='gemini-3.5-flash'` (4 validadores + crítico),
   `MODELO_JUICIO='gemini-3.8-flash'` (scoring, plan, coordinador — verificado disponible con tu key),
   `MAX_ITER_CRITICO=2`, `UMBRAL_TOUCHLESS=25`, `UMBRAL_RETENER=60`, `UMBRAL_MONTO_HITL=10_000.0`,
   `TOLERANCIA_IGV=0.05`, `PRESUPUESTO_LATENCIA_S`, `PRESUPUESTO_TOKENS_FACTURA=60_000`.
2. `nodos/` — `intake.py`, `validadores.py` (4 `LlmAgent`), `scoring.py`, `plan_accion.py`, `cierre.py`.
3. `flujo.py` — el grafo:

```python
VALIDADORES = (validador_tres_vias, validador_tributario,
               validador_duplicados_fraude, validador_proveedor_contrato)
join_validadores = JoinNode(name="join_validadores")

pipeline_ap = Workflow(
    name="pipeline_ap",
    max_concurrency=4,              # los 4 validadores en paralelo → latencia ≈ 1 llamada
    timeout=300.0,
    output_schema=ExpedienteAP,     # nodo_expediente es el único nodo terminal
    edges=[
        (START, nodo_intake),
        (nodo_intake, VALIDADORES),          # fan-out: la tupla genera 4 aristas
        (VALIDADORES, join_validadores),     # fan-in: JoinNode espera a TODOS
        (join_validadores, nodo_consolidar),
        Edge(from_node=nodo_consolidar, to_node=nodo_decision,         route="sin_hallazgos"),
        Edge(from_node=nodo_consolidar, to_node=agente_scoring_riesgo, route="con_hallazgos"),
        (agente_scoring_riesgo, agente_plan_accion, agente_critico, compuerta_critico),
        Edge(from_node=compuerta_critico, to_node=agente_plan_accion, route="reintentar"),
        Edge(from_node=compuerta_critico, to_node=nodo_decision,      route="continuar"),
        (nodo_decision, nodo_expediente),
    ],
)
```

   El ciclo es legal porque contiene una arista enrutada (`workflow/utils/_graph_validation.py:29-62`
   rechaza ciclos 100% incondicionales). **No existe cap de iteraciones en el framework** — verificado:
   no hay `max_iterations`/`max_visits` en todo `workflow/`. El bound es responsabilidad de
   `compuerta_critico`, que incrementa `ctx.state["iter_critico"]` y compara contra `MAX_ITER_CRITICO`.
   El contador se resetea en `nodo_intake`.

4. `coordinador.py` — el puente:

```python
async def triar_factura(id_factura: str, tool_context: ToolContext) -> dict:
    """Ejecuta el pipeline completo de triaje y devuelve el expediente."""
    from .flujo import pipeline_ap                  # import perezoso: evita ciclos
    expediente = await tool_context.run_node(
        pipeline_ap,
        {"id_factura": id_factura},                 # node_input, posicional
        run_id=f"triaje-{id_factura}",              # correlación en traza
    )
    tool_context.state[f"expediente:{id_factura}"] = expediente   # cache: explicar sin re-triar
    return {"estado": "completado", "expediente": expediente}
```

5. `ap_ops/agent.py` (`app = App(...)`) y `ap_ops_pipeline/agent.py`.

**Convención que evita la trampa de `_validate_static_schemas`:** los `FunctionNode` post-join
declaran parámetros nombrados que bindean desde `ctx.state` (`parameter_binding='state'` es el
default) y **no** declaran un parámetro `node_input`. Sin hint de `node_input`, `input_schema` queda
en `None` y el chequeo estático de aristas se salta.

### Fase 3 — Observabilidad (prioridad 1)
1. `plugins/ops_advisor.py` — **tabla de reglas determinista** `dict[TipoSenal, plantilla]` →
   `recomendar(senal) -> RecomendacionOps`. Cero costo LLM en el camino caliente. Un
   `agente_ops_advisor` opcional solo para `tipo='desconocida'`, detrás de `config.OPS_ADVISOR_LLM=False`.
2. `plugins/auditoria.py` — `PluginAuditoriaAP(BasePlugin)`. Hooks: `before_run` (abre
   `RegistroAuditoria`), `after_model` (cuenta llamadas/tokens/latencia vs. presupuesto),
   `on_model_error`, `on_tool_error` (**devuelve la recomendación + instrucción segura al agente:
   "marca `no_evaluable`, no inventes cifras"**), `on_event` (detecta desacuerdo LLM-vs-determinista
   y baja confianza), `after_run` (guarda el registro como artifact). Cada señal pasa por `_emitir()`
   → `ops_advisor.recomendar()` → atributos y eventos de span OTel.
3. `observabilidad/otel.py` — `configurar_otel(modo)`: `"consola"` (`SimpleSpanProcessor` +
   `ConsoleSpanExporter`), `"otlp"` (solo setear `OTEL_EXPORTER_OTLP_ENDPOINT`;
   `telemetry/setup.py:45` lo detecta), `"cloud"` (`get_gcp_exporters(...)` + `get_gcp_resource(...)`
   → `maybe_set_otel_providers(...)`, espejo de `cli/api_server.py:740-778`).
   Equivalente por CLI: `adk web --otel_to_cloud` (`--trace_to_cloud` está deprecado).
4. Métricas propias: contadores `ap_ops.hallazgos{categoria,severidad}`,
   `ap_ops.recomendaciones_ops{tipo_accion}`, `ap_ops.errores_herramienta{tool}`; histogramas
   `ap_ops.puntaje_riesgo`, `ap_ops.tokens_por_factura`, `ap_ops.latencia_nodo{nodo}`.

> Ojo: `adk telemetry enable|disable|status` es el consentimiento de telemetría **de uso de ADK**,
> no OTel. No son lo mismo.

**Prueba negativa (el punto del eje ops):** renombrar `maestro_proveedores.json` y correr una
factura. Debe salir `validador_proveedor_contrato → estado='no_evaluable'` (sin inventar datos),
`RegistroAuditoria` con ≥1 `RecomendacionOps(tipo='error_herramienta', tipo_accion='revisar_fixture')`,
y el pipeline **no** revienta.

### Fase 4 — Deploy (prioridad 2)
**Cloud Run primero**, porque es el único target que funciona con tu key de AI Studio.

```powershell
adk deploy cloud_run `
  --project automatizacionph --region us-central1 `
  --service_name ap-ops --with_ui --port 8080 --otel_to_cloud `
  --env GOOGLE_GENAI_USE_ENTERPRISE=0 `
  --env GOOGLE_API_KEY=$env:GOOGLE_API_KEY `
  --artifact_service_uri "gs://automatizacionph-ap-ops-artifacts" `
  ap_ops
```

⚠️ **`cli/deployers/_cloud_run_deployer.py:259` fuerza `GOOGLE_GENAI_USE_ENTERPRISE=1`** en el env
generado. Hay que sobreescribirlo con `--env GOOGLE_GENAI_USE_ENTERPRISE=0` o el servicio desplegado
intenta Vertex y da 401.

Para persistencia real: `--session_service_uri "postgresql+asyncpg://..."` (Cloud SQL), no sqlite en
`/tmp` que muere con la instancia. Roles para la SA de Cloud Run: `roles/storage.objectAdmin` en el
bucket, más `roles/cloudtrace.agent` + `roles/monitoring.metricWriter` + `roles/logging.logWriter`
para `--otel_to_cloud`.

`adk deploy agent_engine` **requiere Vertex** — queda como Fase 4b si decidís migrar (ver Riesgo 8).

### Fase 5 — HITL (prioridad 3)
1. `App(..., resumability_config=ResumabilityConfig(is_resumable=True))` en **ambos** paquetes.
2. **Nivel coordinador (primario):** `FunctionTool(autorizar_pago, require_confirmation=_requiere_confirmacion)`
   donde el callable devuelve `True` si `monto > UMBRAL_MONTO_HITL`, la decisión no es
   `touchless_approve`, o alguna acción propuesta tiene `reversible=False`. Dentro de la tool:
   `tool_context.request_confirmation(hint=..., payload=...)` en la primera pasada; al reanudar,
   leer `tool_context.tool_confirmation.confirmed/.payload`.
3. **Nivel pipeline (secundario, en `ap_ops_pipeline`):** un `FunctionTool` como nodo detrás de
   `Edge(nodo_decision, herramienta_liberar_pago, route='requiere_hitl')`, auto-envuelto como
   `_ToolNode(rerun_on_resume=True)`.
4. `scripts/resumir_hitl.py` — ciclo completo pausa/reanudación headless:
   `get_request_input_interrupt_ids(event)` → `Runner.run_async(invocation_id=<pausado>,
   new_message=Content(parts=[create_request_input_response(iid, {"confirmed": True, ...})]))`.

### Fase 6 — Evaluación (prioridad 4)
1. **Regresión de expedientes dorados (pytest, barato y filoso).** Por fixture, assert sobre
   `decision.resultado`, el **conjunto** de `Hallazgo.codigo`, `decision.regla_aplicada`, y
   `plan_accion.hallazgos_sin_accion == []`. Acá está la señal real.
2. **`adk eval` para el coordinador** — `eval/test_config.json` como `EvalConfig` con
   `tool_trajectory_avg_score` (las herramientas deterministas **tienen** que ser llamadas: esto
   atrapa directamente "el LLM hizo la aritmética por su cuenta"), `final_response_match_v2`,
   `rubric_based_final_response_quality_v1` (rúbrica en español: ¿propone acciones concretas con
   responsable y SLA?), `hallucinations_v1`, `tool_call_count_v1`. El dict legacy `criteria=` solo
   soporta 4 métricas — las ricas van por `test_config.json`.

---

## Verificación

```powershell
cd "D:\2026 - Proyectos\GCP - Projects\adk-agent1"

# Fase 0
git init; git add -A; git status --short     # sin __pycache__, sin .env, sin *.db
python -m pip install -r requirements.txt
python -c "from google.adk.workflow import Workflow, JoinNode, Edge, START, RetryConfig, node; print('workflow OK')"
python -c "from google.adk.evaluation import AgentEvaluator; print('eval OK')"
python -c "from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter; print('gcp otel OK')"

# Fase 1 — 0 llamadas LLM, 0 costo
python -m ap_ops.scripts.smoke_esquemas       # tabla 7 fixtures × 4 herramientas
python -m pytest ap_ops/tests -q

# Fase 2 — grafo primero (gratis), después LLM
python -c "from ap_ops.flujo import pipeline_ap; pipeline_ap.graph.validate_graph(); print(sorted(n.name for n in pipeline_ap.graph.nodes))"
python -c "import ap_ops.agent"                # reproduce tracebacks que adk web oculta
python -m ap_ops.scripts.correr_pipeline --fixture factura_conforme      --json   # ~4 llamadas
python -m ap_ops.scripts.correr_pipeline --fixture factura_fraude_cuenta --json   # ~7 llamadas
adk web                                        # dropdown: agent_1 / ap_ops / ap_ops_pipeline

# Fase 3
$env:AP_OPS_OTEL="consola"; python -m ap_ops.scripts.correr_pipeline --fixture factura_duplicada
Rename-Item ap_ops\datos\maestro_proveedores.json maestro_proveedores.json.bak   # prueba negativa
python -m ap_ops.scripts.correr_pipeline --fixture factura_conforme
Rename-Item ap_ops\datos\maestro_proveedores.json.bak maestro_proveedores.json
gcloud auth application-default login; adk web --otel_to_cloud

# Fase 5 / 6
python -m ap_ops.scripts.resumir_hitl --fixture factura_fraude_cuenta
adk eval ap_ops ap_ops\eval\triaje_basico.test.json --config_file_path ap_ops\eval\test_config.json
```

**Compuertas de aceptación:** el grafo valida antes de gastar un solo token; los 4 spans de
validador se solapan en tiempo (el fan-out es realmente paralelo); `hallazgos_sin_accion == []`
(eje negocio); `recomendaciones_ops` no vacío al inyectar una falla (eje ops); `llamadas_llm <= 9`
en la peor fixture.

**Específico de Windows:** `adk web --reload` está **forzado a off en win32**
(`cli/cli_tools_click.py:2165-2177`) — hay que reiniciar a mano después de cada edición. La ruta
tiene espacios (`GCP - Projects`): citarla siempre. Lanzar `adk web` desde la raíz del repo.

---

## Riesgos

1. **Trampas de deprecación.** Nunca `SequentialAgent`/`ParallelAgent`/`LoopAgent` (→ `Workflow`);
   nunca `AgentTool` (→ `mode='single_turn'` o `run_node`); nunca `Runner(plugins=...)`
   (→ `App(plugins=...)`); nunca `--trace_to_cloud` (→ `--otel_to_cloud`); nunca
   `google.adk.tools.bigquery` (→ `google.adk.integrations.bigquery`).
2. **`extra='forbid'` en `LlmAgent`.** `temperature=`, `max_output_tokens=`, `instructions=` (plural)
   son `ValidationError` **en tiempo de import**, y `adk web` solo reporta
   `Fail to load 'ap_ops' module`. Los settings de generación van en `generate_content_config`.
   Para ver el traceback real: `python -c "import ap_ops.agent"`.
3. **Nombres de nodo = identificadores Python válidos** (`workflow/_base_node.py:52-60`): sin guiones,
   espacios ni acentos. Nombres duplicados → `GraphValidationError`: reusar la *misma instancia*,
   no dos clones con el mismo nombre (fácil de romper si se construyen validadores en un loop).
4. **Colisiones de estado en paralelo.** Un `output_key` distinto por validador; leer resultados del
   dict del `JoinNode`. `use_sub_branch` aísla **eventos**, no estado — nunca hacer append a una
   lista compartida desde nodos paralelos.
5. **Incompatibilidades Pydantic → `response_schema`.** Sin `Decimal`, `datetime`, `dict[str, Any]`
   ni modelos recursivos en nada que sea `output_schema`. El dinero es `float` en la frontera del
   LLM y `Decimal` dentro de `herramientas/`, o aparecen hallazgos de IGV espurios por drift de float.
6. **Ciclo sin bound = gasto sin bound.** Un ciclo enrutado pasa la validación y **no tiene cap de
   iteraciones en el framework**. `compuerta_critico` es la única defensa, y el contador debe
   resetearse en cada invocación nueva (en `nodo_intake`, no solo en `replantear_plan`).
7. **Retry × ciclo se multiplica.** `retry_config(max_attempts=5)` en un nodo dentro de un ciclo de 2
   iteraciones son hasta 10 llamadas LLM. `retry_config` solo en los 4 validadores, con `exceptions`
   acotadas a errores transitorios y `max_attempts=3`. **Nada de retry en `agente_critico` ni
   `agente_plan_accion`.**
8. **Key de AI Studio vs Vertex — el mayor bloqueador de deploy.** Tu `.env` tiene
   `GOOGLE_GENAI_USE_ENTERPRISE=0` + `GOOGLE_API_KEY`. Ese camino **no** puede usar
   `VertexAiSessionService`, `VertexAiMemoryBankService`, `GcsArtifactService`, `BigQueryToolset` ni
   `adk deploy agent_engine`. Para volverse GCP-native: `gcloud auth application-default login`,
   `GOOGLE_GENAI_USE_ENTERPRISE=1`, soltar la API key, habilitar `aiplatform`/`cloudtrace`/
   `monitoring`/`logging`/`run`/`storage`/`bigquery`, y dar a la SA `roles/aiplatform.user` +
   los 4 roles de observabilidad/storage.
9. **`include_contents='none'` se auto-aplica a los nodos single-turn**
   (`workflow/_llm_agent_wrapper.py:414-416`): un validador no ve la conversación ni a sus hermanos.
   Nunca escribir instrucciones que asuman turnos previos.
10. **HITL no compone a través de `run_node`** desde un `FunctionTool`. Por eso el HITL vive en el
    coordinador y el grafo nunca interrumpe.
11. **Los artifacts necesitan servicio.** `ctx.save_artifact` falla sin uno: en
    `scripts/correr_pipeline.py` pasar `artifact_service=FileArtifactService(root_dir='salidas/artefactos')`
    explícitamente.
12. **`agent_1` no se toca.** `gemini-3.8-flash` es un modelo legítimo — verificado contra la API
    con tu key. El `.gitignore` de raíz le arregla el `__pycache__/` que su propio `.gitignore` no cubre.

---

## LLM vs Python determinista

**Determinista (`herramientas/`), nunca LLM:** validación OCR→`Factura` (Pydantic es mejor y gratis);
dígito verificador RUC mod-11; recálculo de IGV 18%, aritmética de líneas y totales, evaluación de
tolerancias (**la aritmética es el modo de falla #1 de un LLM en AP, y es justo lo que un auditor
va a re-verificar**); ventanas de periodo y tabla de detracciones; deltas de cantidad/precio del
3-way match; huella de duplicado y búsqueda histórica; **comparación de cuenta bancaria/CCI contra
el maestro** (la señal de fraude debe ser exacta, nunca "se parece"); estado del proveedor y consumo
de contrato; dedup de hallazgos + `puntaje_determinista`; **la compuerta de decisión `R-01..R-08`
— un LLM nunca decide liberar dinero**; render del memo; tabla de reglas señal→`RecomendacionOps`.

**Necesita LLM genuinamente:** los 4 validadores (convertir deltas calculados en `Hallazgo` tipados
con severidad *proporcional*, `campo_afectado` correcto, `evidencia` legible, y decir `no_evaluable`
cuando una tool falla); `agente_scoring_riesgo` (correlación cruzada: proveedor nuevo **+** cuenta
cambiada **+** monto justo bajo el umbral es mucho peor que la suma de las partes, más la narrativa
ejecutiva y el flag de desacuerdo); `agente_plan_accion` (**el entregable central**);
`agente_critico` (puntuar un plan contra rúbrica no tiene forma cerrada); `coordinador`.

### Sobre de costo y latencia

| Camino | Llamadas LLM | Nota |
|---|---|---|
| Touchless (la mayoría de facturas) | **4** | `nodo_consolidar` rutea `sin_hallazgos` directo a `nodo_decision`: scoring/plan/crítico se saltan por completo |
| Observada, plan aprobado en primera | **7** | 4 + scoring + plan + crítico |
| Observada, un reintento del crítico | **9** | techo por `MAX_ITER_CRITICO=2` |
| Latencia de pared | ≈ 4 llamadas secuenciales | `max_concurrency=4` colapsa la etapa de validadores a ~1 llamada |

Palancas por orden de retorno: (1) el atajo `sin_hallazgos` — la ganancia más grande, porque la
mayoría de facturas están limpias; (2) una pre-compuerta determinista antes del crítico — si la
cobertura (`todo Hallazgo.codigo aparece en algún AccionPropuesta.codigo_hallazgo`) pasa y hay ≤2
acciones, saltarse el crítico; es una operación de conjuntos, no necesita modelo; (3) tiering de
modelos (`MODELO_RAPIDO` + `temperature=0.0` en validadores y crítico); (4) `explicar_decision` lee
el state cacheado — *"¿por qué retuviste X?"* cuesta **1** llamada, no 7.

---

## Archivos críticos

- `ap_ops/flujo.py` — el grafo: tupla de fan-out, `JoinNode`, ciclo enrutado del crítico, atajo
  `sin_hallazgos`, `max_concurrency=4`, `output_schema=ExpedienteAP`.
- `ap_ops/coordinador.py` — el puente coordinador↔workflow (`tool_context.run_node`) y el HITL.
  Es el archivo donde el veredicto híbrido vive o muere.
- `ap_ops/nodos/validadores.py` — los 4 `LlmAgent` con `output_key` distintos, `output_schema`,
  `retry_config` y `timeout`.
- `ap_ops/plugins/auditoria.py` — el `BasePlugin` que lleva el eje ops (señal → `RecomendacionOps`).
- `ap_ops/herramientas/decision.py` — la compuerta determinista; el único lugar que decide si el
  dinero se mueve.

Fuentes de ADK a releer durante la implementación:
`workflow/_graph.py` (Edge/RoutingMap/matching de rutas), `workflow/utils/_graph_validation.py`
(todos los errores de construcción), `agents/context.py:432` (`run_node`),
`workflow/_dynamic_node_scheduler.py:657-750` (guards y rama standalone),
`workflow/_llm_agent_wrapper.py:292-460` (cómo se comporta un `LlmAgent` como nodo),
`workflow/_tool_node.py` (HITL en grafo).
