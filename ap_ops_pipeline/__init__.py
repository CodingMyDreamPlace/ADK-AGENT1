"""AP Ops — pipeline puro (Workflow sin capa conversacional).

Entry point 2 de los dos que define el plan: expone `pipeline_ap` directamente como
App.root_agent, para batch, evaluacion y para ejercitar el HITL dentro del grafo
(que no compone a traves de run_node desde una FunctionTool). Ver PLAN-AP-OPS.md.
"""

from . import agent  # noqa: F401
