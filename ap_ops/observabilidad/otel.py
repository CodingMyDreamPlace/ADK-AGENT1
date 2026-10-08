"""Configuracion de OpenTelemetry. Tres modos, uno por entorno.

    consola  desarrollo: los spans salen por stdout. Cero infraestructura.
    otlp     un collector propio (Jaeger, Tempo...). Basta con la variable
             OTEL_EXPORTER_OTLP_ENDPOINT: `maybe_set_otel_providers` la detecta
             sola (telemetry/setup.py:45).
    cloud    Cloud Trace / Monitoring / Logging. Requiere ADC y los extras
             [gcp,otel-gcp]. Espejo de lo que hace `adk web --otel_to_cloud`.

NO confundir con `adk telemetry enable|disable`: ese comando maneja el
consentimiento de telemetria de USO de ADK, no OpenTelemetry.

`maybe_set_otel_providers` es no-op si ya hay un provider global: configurar_otel
es idempotente y seguro de llamar mas de una vez.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("ap_ops.otel")

MODOS = ("ninguno", "consola", "otlp", "cloud")


def modo_desde_entorno() -> str:
    """AP_OPS_OTEL=consola|otlp|cloud. Por defecto 'ninguno'."""
    modo = os.environ.get("AP_OPS_OTEL", "ninguno").strip().lower()
    return modo if modo in MODOS else "ninguno"


def configurar_otel(modo: str | None = None, proyecto: str | None = None) -> str:
    """Configura los providers de OTel y devuelve el modo efectivo.

    Nunca levanta: si el modo pedido no se puede configurar (faltan extras, sin
    credenciales) loguea y degrada a 'ninguno'. La observabilidad no puede ser
    la razon por la que no arranca el pipeline.
    """
    modo = (modo or modo_desde_entorno()).lower()
    if modo not in MODOS:
        log.warning("Modo OTel desconocido '%s'; se usa 'ninguno'", modo)
        return "ninguno"
    if modo == "ninguno":
        return "ninguno"

    try:
        from google.adk.telemetry.setup import OTelHooks, maybe_set_otel_providers

        if modo == "consola":
            from opentelemetry.sdk.trace.export import (
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )

            maybe_set_otel_providers(
                [OTelHooks(span_processors=[SimpleSpanProcessor(ConsoleSpanExporter())])]
            )

        elif modo == "otlp":
            if not (
                os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            ):
                log.warning("Modo otlp sin OTEL_EXPORTER_OTLP_ENDPOINT; no se exporta nada")
                return "ninguno"
            maybe_set_otel_providers()

        elif modo == "cloud":
            import google.auth
            from google.adk.telemetry._gcp_resource import get_gcp_resource
            from google.adk.telemetry.google_cloud import get_gcp_exporters

            creds, proyecto_auth = google.auth.default()
            proyecto = proyecto or proyecto_auth
            hooks = get_gcp_exporters(
                enable_cloud_tracing=True,
                enable_cloud_metrics=True,
                enable_cloud_logging=True,
                google_auth=(creds, proyecto),
            )
            maybe_set_otel_providers([hooks], get_gcp_resource(proyecto))

        log.info("OpenTelemetry configurado en modo '%s'", modo)
        return modo

    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo configurar OTel en modo '%s': %s", modo, e)
        return "ninguno"
