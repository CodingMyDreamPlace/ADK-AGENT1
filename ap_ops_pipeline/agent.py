"""Entry point 2: el Workflow puro, sin capa conversacional.

Expone `pipeline_ap` directamente como `App.root_agent`. Es el perfil de
ejecucion para:

  * **batch** — el Cloud Run Job del cierre de mes, disparado por Pub/Sub.
    Un Cloud Run *Service* tiene limite de tiempo por request; procesar 500
    facturas en un solo request no entra, y eso es territorio de *Job*.
  * **evaluacion** — `adk eval` contra una trayectoria deterministica, sin el
    ruido de una conversacion.
  * **HITL dentro del grafo** — un interrupt no compone a traves de
    `run_node` desde una FunctionTool, asi que el HITL en nodo se ejercita
    desde aqui, con `resumability_config` y reanudacion por `invocation_id`.

`pipeline_ap` es un `BaseNode`, no un `BaseAgent`, y `App.root_agent` acepta
`BaseNode` (`apps/app.py:74`), igual que el loader del CLI
(`cli/utils/agent_loader.py:138,187`).
"""

from __future__ import annotations

from google.adk.apps import App
from google.adk.apps._configs import ResumabilityConfig

from ap_ops.flujo import pipeline_ap

root_agent = pipeline_ap

app = App(
    name="ap_ops_pipeline",
    root_agent=pipeline_ap,
    # Requisito para pausar y reanudar por `invocation_id`. Se activa desde ya
    # para que Fase 5 no tenga que cambiar la forma de la App.
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=[],
)
