"""Aprobacion humana: politica, herramienta y ciclo REAL pausa -> confirmacion.

CERO llamadas al modelo. El ciclo completo corre sobre el Runner de ADK con un
LLM falso que emite el function call, asi que verifica la mecanica del
framework (que la invocacion se PAUSE de verdad y se reanude con la respuesta
humana) sin gastar cuota.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.apps._configs import ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from ap_ops.coordinador import autorizar_pago, coordinador
from ap_ops.herramientas.autorizacion import evaluar_autorizacion
from ap_ops.scripts.smoke_esquemas import procesar

ID = "20512345671-F001-00241"  # factura_igv_erroneo


def expediente(fixture="factura_igv_erroneo") -> dict:
    return procesar(fixture)


# =================================================================== politica
class TestPoliticaDeAutorizacion:
    def test_sin_expediente_no_se_puede(self):
        r = evaluar_autorizacion(None)
        assert not r["permitido"] and "triar_factura" in r["razon"]

    def test_aprobada_con_condiciones_es_autorizable(self):
        r = evaluar_autorizacion(expediente("factura_igv_erroneo"))
        assert r["permitido"] and r["monto"] == 11700.0 and r["moneda"] == "PEN"

    def test_touchless_es_autorizable(self):
        assert evaluar_autorizacion(expediente("factura_conforme"))["permitido"]

    def test_el_monto_sale_del_expediente_nunca_de_un_argumento(self):
        """El LLM elige QUE factura; jamas CUANTO."""
        import inspect

        assert "monto" not in inspect.signature(autorizar_pago).parameters
        assert "monto" not in inspect.signature(evaluar_autorizacion).parameters

    @pytest.mark.parametrize(
        "fixture,fragmento",
        [
            ("factura_duplicada", "RECHAZADA"),          # R-03
            ("factura_fraude_cuenta", "bloqueantes"),     # R-02, FRA-CTA-004
            ("factura_sin_oc", "bloqueantes"),            # R-04, TRV-OC-001
            ("factura_contrato_excedido", "bloqueantes"), # R-04, PRV-LIM-004
            ("factura_ocr_sucio", "no pudieron concluir"),# R-01
        ],
    )
    def test_lo_bloqueado_no_se_salta_con_un_si(self, fixture, fragmento):
        """Un humano apurado firmando encima de un duplicado o de una cuenta
        desviada es el incidente que el sistema existe para evitar."""
        r = evaluar_autorizacion(expediente(fixture), motivo="urgente, lo pide el gerente")
        assert r["permitido"] is False
        assert fragmento in r["razon"]
        assert r["monto"] == 0.0

    def test_retenida_por_riesgo_exige_motivo_por_escrito(self):
        e = expediente("factura_igv_erroneo")
        e["decision"]["resultado"] = "retener"
        e["decision"]["regla_aplicada"] = "R-05"
        sin = evaluar_autorizacion(e, motivo="")
        assert not sin["permitido"] and sin["requiere_motivo"]
        con = evaluar_autorizacion(e, motivo="Proveedor critico, verificado por telefono")
        assert con["permitido"] and con["requiere_motivo"]

    def test_un_motivo_en_blanco_no_cuenta(self):
        e = expediente("factura_igv_erroneo")
        e["decision"]["resultado"] = "retener"
        assert not evaluar_autorizacion(e, motivo="   ")["permitido"]


# ========================================================== herramienta (aislada)
def ctx_falso(expedientes: dict | None = None, confirmacion=None):
    """ToolContext minimo: lo que `autorizar_pago` realmente toca."""
    pedidos = []
    state = {}
    for k, v in (expedientes or {}).items():
        state[f"expediente:{k}"] = v
    return SimpleNamespace(
        state=state,
        tool_confirmation=confirmacion,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=lambda **kw: pedidos.append(kw),
        pedidos=pedidos,
    )


def run(coro):
    return asyncio.run(coro)


class TestHerramienta:
    def test_primera_pasada_pide_confirmacion_con_hint_rico(self):
        c = ctx_falso({ID: expediente()})
        r = run(autorizar_pago(ID, "", c))
        assert r == {"estado": "pendiente_confirmacion"}
        assert len(c.pedidos) == 1
        hint = c.pedidos[0]["hint"]
        # El humano debe ver QUE esta firmando, no un texto generico.
        for esperado in ("11,700.00", "ACME", "20512345671", "R-06", "TRI-IGV-001", "no se puede deshacer"):
            assert esperado in hint, esperado
        assert c.pedidos[0]["payload"]["monto"] == 11700.0
        assert c.actions.skip_summarization is True
        assert f"autorizacion:{ID}" not in c.state, "no se registra nada antes de confirmar"

    def test_la_cuenta_va_enmascarada_en_el_hint(self):
        e = expediente()
        e["resultados"] = {"duplicados_fraude": {"cuenta_declarada_enmascarada": "****9012"}}
        c = ctx_falso({ID: e})
        run(autorizar_pago(ID, "", c))
        assert "****9012" in c.pedidos[0]["hint"]
        assert "00219300123456789012" not in c.pedidos[0]["hint"]

    def test_confirmada_registra_la_autorizacion_sin_mover_dinero(self):
        c = ctx_falso({ID: expediente()}, SimpleNamespace(confirmed=True, payload={}))
        r = run(autorizar_pago(ID, "", c))
        reg = c.state[f"autorizacion:{ID}"]
        assert r["estado"] == "autorizado" and reg["estado"] == "autorizado"
        assert reg["monto"] == 11700.0
        assert reg["pendiente_de_ejecucion_en_erp"] is True, "el agente no ejecuta el pago"
        assert not c.pedidos

    def test_rechazada_por_el_humano_queda_registrada(self):
        c = ctx_falso(
            {ID: expediente()}, SimpleNamespace(confirmed=False, payload={"motivo": "falta OC"})
        )
        r = run(autorizar_pago(ID, "", c))
        assert r["estado"] == "rechazado_por_humano"
        assert c.state[f"autorizacion:{ID}"]["motivo"] == "falta OC"

    def test_idempotente_no_se_autoriza_dos_veces(self):
        """La falla catastrofica de AP: pagar dos veces."""
        c = ctx_falso({ID: expediente()}, SimpleNamespace(confirmed=True, payload={}))
        run(autorizar_pago(ID, "", c))
        c2 = ctx_falso({ID: expediente()})
        c2.state = c.state
        r = run(autorizar_pago(ID, "", c2))
        assert r["estado"] == "ya_autorizado"
        assert not c2.pedidos, "no debe volver a preguntar"

    def test_denegado_no_molesta_al_humano(self):
        c = ctx_falso({"X": expediente("factura_duplicada")})
        r = run(autorizar_pago("X", "", c))
        assert r["estado"] == "denegado" and not c.pedidos

    def test_sin_expediente_deniega(self):
        r = run(autorizar_pago("NOEXISTE", "", ctx_falso()))
        assert r["estado"] == "denegado"

    def test_la_politica_se_reevalua_al_reanudar(self):
        """Defensa en profundidad: aunque el humano diga que si, si el expediente
        paso a ser bloqueante entre la pregunta y la respuesta, no se autoriza."""
        c = ctx_falso({ID: expediente("factura_duplicada")}, SimpleNamespace(confirmed=True, payload={}))
        r = run(autorizar_pago(ID, "", c))
        assert r["estado"] == "denegado"
        assert f"autorizacion:{ID}" not in c.state

    def test_un_rechazo_previo_no_bloquea_reintentar(self):
        c = ctx_falso({ID: expediente()})
        c.state[f"autorizacion:{ID}"] = {"estado": "rechazado_por_humano"}
        assert run(autorizar_pago(ID, "", c))["estado"] == "pendiente_confirmacion"


# ======================================================== ciclo REAL del framework
class LlmGuionado(BaseLlm):
    """LLM falso: emite un function call y luego texto. Sin red, sin cuota."""

    model: str = "falso"
    respuestas: list = []

    async def generate_content_async(self, llm_request, stream=False):
        yield self.respuestas.pop(0)


def fc(nombre, **args):
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=nombre, args=args))])
    )


def texto(t):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]))


APP = "t_hitl"


async def _correr(runner, sid, parts, **kw):
    eventos = []
    async for ev in runner.run_async(
        user_id="u", session_id=sid, new_message=types.Content(role="user", parts=parts), **kw
    ):
        eventos.append(ev)
    return eventos


def _pedido_de_confirmacion(eventos):
    for ev in eventos:
        for p in (ev.content.parts if ev.content else []) or []:
            if p.function_call and p.function_call.name == "adk_request_confirmation":
                return p.function_call
    return None


def _armar(exp, respuestas):
    agente = LlmAgent(
        name="a",
        model=LlmGuionado(respuestas=respuestas),
        instruction="x",
        tools=[FunctionTool(autorizar_pago)],
    )
    app = App(name=APP, root_agent=agente, resumability_config=ResumabilityConfig(is_resumable=True))
    svc = InMemorySessionService()
    runner = Runner(app=app, session_service=svc)
    return runner, svc


async def _sesion(svc, exp, id_=ID):
    await svc.create_session(
        app_name=APP, user_id="u", session_id="s", state={f"expediente:{id_}": exp}
    )


async def _estado(svc, clave):
    s = await svc.get_session(app_name=APP, user_id="u", session_id="s")
    return s.state.get(clave)


class TestCicloRealDelFramework:
    def test_pausa_y_se_reanuda_con_la_aprobacion_humana(self):
        async def caso():
            runner, svc = _armar(None, [fc("autorizar_pago", id_factura=ID, motivo=""), texto("listo")])
            await _sesion(svc, expediente())

            # --- 1) el modelo pide autorizar -> el framework PAUSA ----------
            ev1 = await _correr(runner, "s", [types.Part(text="autoriza el pago")])
            pedido = _pedido_de_confirmacion(ev1)
            assert pedido is not None, "el framework debio emitir adk_request_confirmation"
            assert await _estado(svc, f"autorizacion:{ID}") is None, (
                "NADA debe registrarse mientras espera al humano"
            )

            # --- 2) la persona aprueba -> se reanuda y se registra ----------
            respuesta = types.Part(
                function_response=types.FunctionResponse(
                    id=pedido.id, name="adk_request_confirmation", response={"confirmed": True}
                )
            )
            await _correr(runner, "s", [respuesta])
            reg = await _estado(svc, f"autorizacion:{ID}")
            assert reg and reg["estado"] == "autorizado" and reg["monto"] == 11700.0
            assert reg["pendiente_de_ejecucion_en_erp"] is True

        asyncio.run(caso())

    def test_si_la_persona_rechaza_no_se_autoriza(self):
        async def caso():
            runner, svc = _armar(None, [fc("autorizar_pago", id_factura=ID, motivo=""), texto("ok")])
            await _sesion(svc, expediente())
            pedido = _pedido_de_confirmacion(await _correr(runner, "s", [types.Part(text="autoriza")]))
            assert pedido is not None
            no = types.Part(
                function_response=types.FunctionResponse(
                    id=pedido.id, name="adk_request_confirmation",
                    response={"confirmed": False, "payload": {"motivo": "falta OC"}},
                )
            )
            await _correr(runner, "s", [no])
            reg = await _estado(svc, f"autorizacion:{ID}")
            assert reg["estado"] == "rechazado_por_humano"
            assert reg["motivo"] == "falta OC"

        asyncio.run(caso())

    def test_una_factura_bloqueada_nunca_llega_a_pedir_confirmacion(self):
        async def caso():
            dup = expediente("factura_duplicada")
            i = "20512345671-F001-00201"
            runner, svc = _armar(None, [fc("autorizar_pago", id_factura=i, motivo="por favor"), texto("no")])
            await _sesion(svc, dup, i)
            ev = await _correr(runner, "s", [types.Part(text="autoriza")])
            assert _pedido_de_confirmacion(ev) is None, "no se debe molestar al humano"
            assert await _estado(svc, f"autorizacion:{i}") is None

        asyncio.run(caso())


class TestIntegracion:
    def test_el_coordinador_expone_autorizar_pago(self):
        nombres = {getattr(t, "__name__", getattr(t, "name", "")) for t in coordinador.tools}
        assert "autorizar_pago" in nombres

    def test_el_coordinador_esta_en_una_app_reanudable(self):
        from ap_ops.agent import app

        assert app.resumability_config and app.resumability_config.is_resumable

    def test_la_instruccion_prohibe_decir_que_se_pago(self):
        ins = coordinador.instruction.lower()
        assert "nunca que se autorizo" in ins and "no que se pago" in ins
