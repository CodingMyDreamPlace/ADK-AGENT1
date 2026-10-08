"""Plugin de auditoria: telemetria tecnica + traza de negocio en un solo registro.

Acumula un `RegistroAuditoria` por invocacion y, ante cada anomalia, emite una
`RecomendacionOps` accionable (via la tabla de `ops_advisor`), no una linea de
log. Mantener tecnica y negocio en el mismo objeto es deliberado: la pregunta
real nunca es "cuanto tardo el nodo X" ni "por que se retuvo la factura Y" por
separado, sino "por que esta factura tardo 40 s y termino retenida". Ese cruce
solo se responde si ambos lados comparten el `invocation_id`.

REGLA DE ORO: un plugin de observabilidad NO PUEDE ser una fuente de fallas.
Todo hook esta envuelto en `_seguro`: si la auditoria explota, el pipeline
sigue. Un sistema de monitoreo que tumba lo que monitorea es peor que ninguno.

Los hooks devuelven `None` salvo `on_tool_error_callback`, que es el unico que
interviene: convierte un error de herramienta en una respuesta estructurada que
le dice al validador que marque `no_evaluable` y no invente cifras.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import datetime, timezone
from typing import Any

from google.adk.plugins import BasePlugin
from google.genai import types

from .. import config
from ..esquemas import DatoVerificado, RegistroAuditoria, SenalOps, Severidad
from . import ops_advisor

log = logging.getLogger("ap_ops.auditoria")

#: Diferencia de puntaje LLM vs determinista que se considera desacuerdo.
UMBRAL_DESACUERDO = 20
#: Confianza del modelo por debajo de la cual se manda a revision humana.
UMBRAL_BAJA_CONFIANZA = 0.6
#: Intentos de un mismo nodo a partir de los cuales hay "tormenta de reintentos".
UMBRAL_TORMENTA = 2


def _seguro(fn):
    """Los hooks de auditoria jamas propagan excepciones."""

    @functools.wraps(fn)
    async def envoltura(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("PluginAuditoriaAP.%s fallo; el pipeline continua", fn.__name__)
            return None

    return envoltura


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PluginAuditoriaAP(BasePlugin):
    """Audita cada invocacion y convierte anomalias en recomendaciones."""

    def __init__(self) -> None:
        super().__init__(name="auditoria_ap")
        self._registros: dict[str, RegistroAuditoria] = {}
        self._t_modelo: dict[tuple[str, str], float] = {}
        self._vistas: dict[str, set[tuple[str, str]]] = {}
        #: Registros ya cerrados. Los consumen el script de corrida y los tests.
        self.cerrados: list[RegistroAuditoria] = []

    # ------------------------------------------------------------ nucleo
    def _registro(self, invocation_id: str) -> RegistroAuditoria:
        reg = self._registros.get(invocation_id)
        if reg is None:
            reg = RegistroAuditoria(invocation_id=invocation_id, inicio=_ahora())
            self._registros[invocation_id] = reg
        return reg

    def emitir(
        self,
        invocation_id: str,
        tipo: str,
        severidad: Severidad,
        componente: str,
        detalle: str,
        metricas: list[DatoVerificado] | None = None,
        id_factura: str | None = None,
    ):
        """Registra una senal y su recomendacion. Deduplica por (tipo, componente).

        La deduplicacion importa: un reintento dispara el mismo hook varias
        veces, y diez recomendaciones identicas serian ruido que entierra la
        unica que importa.
        """
        reg = self._registro(invocation_id)
        clave = (tipo, componente)
        vistas = self._vistas.setdefault(invocation_id, set())
        if clave in vistas:
            return None
        vistas.add(clave)

        if id_factura and not reg.id_factura:
            reg.id_factura = id_factura

        senal = SenalOps(
            tipo=tipo,  # type: ignore[arg-type]
            severidad=severidad,
            componente=componente,
            detalle=detalle[:500],
            metricas=metricas or [],
            invocation_id=invocation_id,
            id_factura=reg.id_factura,
        )
        rec = ops_advisor.recomendar(senal)
        reg.recomendaciones_ops.append(rec)
        self._anotar_span(rec)
        return rec

    @staticmethod
    def _anotar_span(rec) -> None:
        """Adjunta la recomendacion al span activo para verla en Cloud Trace."""
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            if span is None or not span.is_recording():
                return
            span.set_attribute("ap_ops.senal", rec.senal.tipo)
            span.set_attribute("ap_ops.accion_ops", rec.tipo_accion)
            span.set_attribute("ap_ops.urgencia", rec.urgencia)
            span.add_event(
                "ap_ops.recomendacion",
                {"accion": rec.accion_recomendada[:200], "runbook": rec.runbook},
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _id_factura(ctx: Any) -> str | None:
        try:
            return ctx.state.get("id_factura")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ ciclo de vida
    @_seguro
    async def before_run_callback(self, *, invocation_context):
        self._registro(invocation_context.invocation_id)
        return None

    @_seguro
    async def after_run_callback(self, *, invocation_context):
        inv = invocation_context.invocation_id
        reg = self._registros.pop(inv, None)
        self._vistas.pop(inv, None)
        if reg is None:
            return None

        reg.fin = _ahora()
        try:
            reg.duracion_s = round(
                (
                    datetime.fromisoformat(reg.fin) - datetime.fromisoformat(reg.inicio)
                ).total_seconds(),
                2,
            )
        except ValueError:
            pass

        total_tokens = reg.tokens_entrada + reg.tokens_salida
        if total_tokens > config.PRESUPUESTO_TOKENS_FACTURA:
            self._registros[inv] = reg  # emitir() necesita el registro vivo
            self.emitir(
                inv,
                "gasto_tokens_alto",
                Severidad.MEDIA,
                "invocacion",
                f"{total_tokens:,} tokens > presupuesto "
                f"{config.PRESUPUESTO_TOKENS_FACTURA:,}",
                [DatoVerificado(nombre="tokens", valor=str(total_tokens))],
            )
            self._registros.pop(inv, None)
            self._vistas.pop(inv, None)

        try:
            reg.decision_final = invocation_context.session.state.get("decision", {}).get(
                "resultado"
            )
        except Exception:  # noqa: BLE001
            pass

        self.cerrados.append(reg)
        await self._guardar_artifact(invocation_context, reg)
        return None

    @staticmethod
    async def _guardar_artifact(invocation_context, reg: RegistroAuditoria) -> None:
        svc = getattr(invocation_context, "artifact_service", None)
        if svc is None:
            return
        try:
            await svc.save_artifact(
                app_name=invocation_context.app_name,
                user_id=invocation_context.user_id,
                session_id=invocation_context.session.id,
                filename=f"auditoria/{reg.invocation_id}.json",
                artifact=types.Part.from_bytes(
                    data=reg.model_dump_json(indent=2).encode("utf-8"),
                    mime_type="application/json",
                ),
            )
        except Exception:  # noqa: BLE001
            log.warning("No se pudo guardar el registro de auditoria", exc_info=True)

    # ------------------------------------------------------------ modelo
    @_seguro
    async def before_model_callback(self, *, callback_context, llm_request):
        self._t_modelo[(callback_context.invocation_id, callback_context.agent_name)] = (
            time.monotonic()
        )
        return None

    @_seguro
    async def after_model_callback(self, *, callback_context, llm_response):
        inv, agente = callback_context.invocation_id, callback_context.agent_name
        reg = self._registro(inv)
        reg.llamadas_llm += 1

        um = getattr(llm_response, "usage_metadata", None)
        if um is not None:
            reg.tokens_entrada += getattr(um, "prompt_token_count", 0) or 0
            reg.tokens_salida += getattr(um, "candidates_token_count", 0) or 0

        t0 = self._t_modelo.pop((inv, agente), None)
        if t0 is not None:
            dt = time.monotonic() - t0
            reg.latencias_por_nodo.append(DatoVerificado(nombre=agente, valor=f"{dt:.2f}s"))
            presupuesto = config.PRESUPUESTO_LATENCIA_S.get(agente, 30.0)
            if dt > presupuesto:
                self.emitir(
                    inv,
                    "latencia_alta",
                    Severidad.MEDIA,
                    agente,
                    f"{dt:.1f}s supera el presupuesto de {presupuesto:.0f}s",
                    [
                        DatoVerificado(nombre="latencia_s", valor=f"{dt:.2f}"),
                        DatoVerificado(nombre="presupuesto_s", valor=f"{presupuesto:.0f}"),
                    ],
                    self._id_factura(callback_context),
                )
        return None

    @_seguro
    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        texto = repr(error)
        # 429/503 son del proveedor y el retry del nodo los absorbe; un error
        # distinto es mas grave porque probablemente no se arregla reintentando.
        transitorio = any(c in texto for c in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"))
        self.emitir(
            callback_context.invocation_id,
            "error_modelo",
            Severidad.ALTA if transitorio else Severidad.CRITICA,
            callback_context.agent_name,
            texto,
            id_factura=self._id_factura(callback_context),
        )
        return None  # None: que el retry_config del nodo haga su trabajo

    # ------------------------------------------------------------ herramientas
    @_seguro
    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        self._registro(tool_context.invocation_id).llamadas_herramienta += 1
        return None

    @_seguro
    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        inv = tool_context.invocation_id
        reg = self._registro(inv)
        reg.errores_herramienta += 1
        reg.llamadas_herramienta += 1

        rec = self.emitir(
            inv,
            "error_herramienta",
            Severidad.ALTA,
            f"{tool.name}@{getattr(tool_context, 'agent_name', '?')}",
            repr(error),
            id_factura=self._id_factura(tool_context),
        )

        intento = getattr(tool_context, "attempt_count", 1) or 1
        if intento > UMBRAL_TORMENTA:
            reg.reintentos += 1
            self.emitir(
                inv,
                "tormenta_reintentos",
                Severidad.ALTA,
                tool.name,
                f"intento {intento} de la misma herramienta",
                [DatoVerificado(nombre="intento", valor=str(intento))],
                self._id_factura(tool_context),
            )

        # La unica intervencion del plugin: el error se convierte en una
        # respuesta que le dice al validador exactamente que hacer. Sin esto el
        # modelo ve una excepcion cruda y tiende a rellenar con cifras propias.
        return {
            "datos_disponibles": False,
            "error": str(error),
            "desviaciones": [],
            "instruccion_para_el_agente": (
                "La herramienta fallo. Responde estado='no_evaluable', "
                "hallazgos=[] y explica en `resumen` que falto. NO INVENTES CIFRAS."
            ),
            "accion_recomendada": rec.accion_recomendada if rec else None,
        }

    # ------------------------------------------------------------ eventos
    @_seguro
    async def on_event_callback(self, *, invocation_context, event):
        salida = getattr(event, "output", None)
        if not isinstance(salida, dict):
            return None
        inv = invocation_context.invocation_id
        autor = getattr(event, "author", "?")

        # --- evaluacion de riesgo: desacuerdo LLM vs determinista -----------
        if "puntaje_riesgo" in salida and "puntaje_determinista" in salida:
            delta = abs(int(salida["puntaje_riesgo"]) - int(salida["puntaje_determinista"]))
            if salida.get("desacuerdo_con_determinista") or delta > UMBRAL_DESACUERDO:
                self.emitir(
                    inv,
                    "desacuerdo_validadores",
                    Severidad.ALTA,
                    autor,
                    f"modelo={salida['puntaje_riesgo']} vs "
                    f"determinista={salida['puntaje_determinista']} (delta {delta})",
                    [DatoVerificado(nombre="delta", valor=str(delta))],
                )
            if float(salida.get("confianza", 1.0)) < UMBRAL_BAJA_CONFIANZA:
                self.emitir(
                    inv,
                    "decision_baja_confianza",
                    Severidad.MEDIA,
                    autor,
                    f"confianza={salida['confianza']}",
                )

        # --- validador que no pudo concluir ---------------------------------
        if salida.get("estado") == "no_evaluable":
            self.emitir(
                inv,
                "validador_no_evaluable",
                Severidad.MEDIA,
                str(salida.get("validador", autor)),
                str(salida.get("resumen", "sin detalle")),
            )

        # --- ciclo del critico agotado --------------------------------------
        if salida.get("ciclo_agotado") and not salida.get("aprobado"):
            self.emitir(
                inv,
                "ciclo_critico_agotado",
                Severidad.MEDIA,
                autor,
                f"sin plan aprobado tras {salida.get('iteracion')} vuelta(s)",
            )
        return None
