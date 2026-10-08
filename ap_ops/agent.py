"""Entry point 1: el coordinador conversacional.

ADK descubre los agentes buscando, en `<agents_dir>/<paquete>/agent.py`, primero
una variable `app` y despues `root_agent` (`cli/utils/agent_loader.py:120-200`).
Exponemos `app` porque un `App` es lo que permite registrar plugins; pasar
`plugins=` al `Runner` esta deprecado en 2.11.

    adk web          -> dropdown con agent_1 / ap_ops / ap_ops_pipeline
    adk run ap_ops   -> consola
"""

from __future__ import annotations

from google.adk.apps import App

from .coordinador import coordinador
from .observabilidad.otel import configurar_otel
from .plugins import PluginAuditoriaAP

# AP_OPS_OTEL=consola|otlp|cloud. Sin la variable es no-op. Nunca levanta.
configurar_otel()

#: `root_agent` se expone ademas de `app` por compatibilidad: varios tutoriales
#: y herramientas de terceros lo buscan por nombre.
root_agent = coordinador

app = App(
    name="ap_ops",
    root_agent=coordinador,
    # El plugin convierte cada senal de observabilidad en una RecomendacionOps
    # accionable. Se registra en el App y no en el Runner: `Runner(plugins=...)`
    # esta deprecado en 2.11.
    plugins=[PluginAuditoriaAP()],
)
