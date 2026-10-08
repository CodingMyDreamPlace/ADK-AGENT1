"""Corre el pipeline completo sobre una factura. ESTE SI GASTA TOKENS.

Es el primer script del proyecto que llama a Gemini. Hasta Fase 1b todo era
determinista y gratis; de aqui en adelante cada corrida cuesta.

    python -m ap_ops.scripts.correr_pipeline --fixture factura_conforme
    python -m ap_ops.scripts.correr_pipeline --fixture factura_fraude_cuenta --json
    python -m ap_ops.scripts.correr_pipeline --fixture factura_conforme --memo
    python -m ap_ops.scripts.correr_pipeline --listar

Costo medido (no estimado) por corrida:
    factura limpia     8 llamadas  (4 validadores x 2) — atajo touchless
    factura observada 11 llamadas  (8 + scoring + plan + critico)
    con un reintento  13 llamadas  (techo por MAX_ITER_CRITICO)

Por que 2 llamadas por validador y no 1: un agente con `tools` hace una primera
llamada que emite el function call, y una segunda que devuelve el JSON
estructurado despues de que la herramienta respondio. Los agentes SIN tools
(scoring, plan, critico) usan `response_schema` nativo y gastan una sola.

Ese factor 2 es lo que hace que el fan-out de 4 validadores sean 8 requests en
rafaga, y lo que choca contra el limite de 5 requests/minuto del free tier de
AI Studio. Ver `config.PERFIL_CUOTA`.

Usa `Runner(app=...)` con el Workflow como `root_agent`, que es el mismo camino
que toma `ap_ops_pipeline` en Cloud Run. El id de la factura viaja por
`state_delta`, no por el mensaje: `nodo_intake` acepta las dos vias.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path  # noqa: I001  (lo usa _cargar_env, definido abajo)

os.environ.setdefault("AP_OPS_FECHA_REFERENCIA", "2026-10-03")


def _cargar_env() -> None:
    """Carga `ap_ops/.env`.

    ADK carga el `.env` POR PAQUETE de agente, pero solo cuando el CLI es el
    que arranca (`load_dotenv_for_agent` en `cli/utils/envs.py`). Un
    `python -m ap_ops.scripts...` no pasa por ahi, asi que sin esto el cliente
    de Gemini se construye sin credenciales y los cuatro validadores fallan con
    'No API key was provided'.

    Las variables ya presentes en el entorno NO se sobreescriben: eso permite
    que Cloud Run inyecte las suyas por `--env` sin que un `.env` empaquetado
    se las pise.
    """
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


_cargar_env()

from google.adk.apps import App  # noqa: E402
from google.adk.artifacts import FileArtifactService  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from ..flujo import pipeline_ap  # noqa: E402
from ..herramientas import listar_facturas, render_memo_markdown  # noqa: E402
from ..observabilidad.otel import configurar_otel  # noqa: E402
from ..plugins import PluginAuditoriaAP  # noqa: E402

configurar_otel()  # AP_OPS_OTEL=consola para ver los spans por stdout

DIR_SALIDAS = Path("salidas")


async def correr(id_factura: str, verbose: bool = True) -> tuple[dict | None, dict]:
    """Ejecuta el pipeline y devuelve (expediente, metricas)."""
    auditoria = PluginAuditoriaAP()
    app = App(name="ap_ops_pipeline", root_agent=pipeline_ap, plugins=[auditoria])
    runner = Runner(
        app=app,
        session_service=InMemorySessionService(),
        artifact_service=FileArtifactService(root_dir=str(DIR_SALIDAS / "artefactos")),
        auto_create_session=True,
    )

    metricas = {
        "llamadas_llm": 0,
        "tokens_entrada": 0,
        "tokens_salida": 0,
        "nodos": [],
        "errores": [],
    }
    expediente: dict | None = None
    t0 = time.monotonic()

    async for event in runner.run_async(
        user_id="smoke",
        session_id=f"ses-{id_factura}",
        # El mensaje lleva solo el id: el Runner lo entrega como `node_input`
        # del Workflow, y `nodo_intake` lo resuelve igual que el `state_delta`.
        new_message=types.Content(role="user", parts=[types.Part(text=id_factura)]),
        state_delta={"id_factura": id_factura},
    ):
        autor = getattr(event, "author", "?")

        um = getattr(event, "usage_metadata", None)
        if um:
            metricas["llamadas_llm"] += 1
            metricas["tokens_entrada"] += um.prompt_token_count or 0
            metricas["tokens_salida"] += um.candidates_token_count or 0
            if verbose:
                print(
                    f"  [llm ] {autor:30} "
                    f"in={um.prompt_token_count or 0:>6} "
                    f"out={um.candidates_token_count or 0:>5}"
                )

        salida = getattr(event, "output", None)
        if salida is not None:
            metricas["nodos"].append(autor)
            if isinstance(salida, dict) and "id_expediente" in salida:
                expediente = salida
            if verbose and not um:
                etiqueta = ""
                if isinstance(salida, dict):
                    if "ruta" in salida:
                        etiqueta = f" ruta={salida['ruta']}"
                    elif "regla_aplicada" in salida:
                        etiqueta = f" {salida['regla_aplicada']} {salida.get('resultado', '')}"
                print(f"  [nodo] {autor:30}{etiqueta}")

        if getattr(event, "error_message", None):
            metricas["errores"].append(f"{autor}: {event.error_message}")
            if verbose:
                print(f"  [ERR ] {autor}: {event.error_message}")

    metricas["duracion_s"] = round(time.monotonic() - t0, 2)
    # Eje ops: lo que el plugin de auditoria detecto durante la corrida.
    metricas["recomendaciones_ops"] = [
        r for reg in auditoria.cerrados for r in reg.recomendaciones_ops
    ]
    return expediente, metricas


def imprimir_resumen(expediente: dict | None, metricas: dict) -> None:
    print()
    print("-" * 78)
    if expediente is None:
        print("  El pipeline NO devolvio expediente.")
        for e in metricas["errores"]:
            print(f"    error: {e}")
        return

    f = expediente["factura"]
    d = expediente["decision"]
    ev = expediente["evaluacion"]
    cons = expediente["consolidado"]
    plan = expediente["plan_accion"]

    print(f"  Factura   {expediente['id_factura']}  {f['razon_social']}")
    print(f"  Total     {f['moneda']} {f['total']:,.2f}")
    print(f"  Decision  {d['resultado'].upper()}   regla {d['regla_aplicada']}")
    print(
        f"  Riesgo    {ev['puntaje_riesgo']}/100 "
        f"(determinista {cons['puntaje_determinista']}/100"
        f"{', DESACUERDO' if ev.get('desacuerdo_con_determinista') else ''})"
    )
    print(f"  Confirmacion humana: {'SI' if d['requiere_confirmacion_humana'] else 'no'}")
    print()

    if cons["hallazgos"]:
        print(f"  Hallazgos ({len(cons['hallazgos'])}):")
        for h in cons["hallazgos"]:
            bloq = " [BLOQUEA PAGO]" if h.get("bloquea_pago") else ""
            print(f"    {h['codigo']:14} {str(h['severidad']):11} {h['titulo']}{bloq}")
    else:
        print("  Sin hallazgos (ruta touchless)")
    print()

    if plan["acciones"]:
        print(f"  Acciones ({len(plan['acciones'])}):")
        for a in sorted(plan["acciones"], key=lambda x: x.get("prioridad", 99)):
            print(
                f"    P{a['prioridad']} {a['responsable']:16} {a['sla_horas']:>3}h  "
                f"{a['accion']}"
            )
    else:
        print("  Sin acciones")

    print()
    print("  --- INVARIANTE 'ante una observacion, una accion' ---")
    print(f"    hallazgos sin accion : {plan['hallazgos_sin_accion'] or 'ninguno  OK'}")
    print(f"    acciones sin hallazgo: {plan['acciones_sin_hallazgo'] or 'ninguna  OK'}")

    print()
    print("  --- COSTO ---")
    print(
        f"    llamadas al modelo : {metricas['llamadas_llm']}  "
        f"(tokens {metricas['tokens_entrada']:,} in / "
        f"{metricas['tokens_salida']:,} out)"
    )
    print(f"    iteraciones critico: {expediente['iteraciones_critico']}")
    print(f"    duracion           : {metricas['duracion_s']} s")
    print(f"    memo               : {expediente.get('artefacto_memo') or 'no guardado'}")

    recs = metricas.get("recomendaciones_ops", [])
    print()
    print(f"  --- EJE OPS: recomendaciones ({len(recs)}) ---")
    if not recs:
        print("    ninguna: el pipeline corrio sin anomalias")
    for r in recs:
        print(f"    [{r.urgencia.upper():5}] {r.senal.tipo} @ {r.senal.componente}")
        print(f"            -> {r.tipo_accion} ({r.responsable}): {r.accion_recomendada[:110]}")
    print("-" * 78)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Corre el pipeline AP Ops. GASTA TOKENS.")
    p.add_argument("--fixture", help="nombre del fixture o id_factura")
    p.add_argument("--json", action="store_true", help="volcar el expediente a JSON")
    p.add_argument("--memo", action="store_true", help="imprimir el memo de auditoria")
    p.add_argument("--listar", action="store_true", help="listar fixtures disponibles")
    p.add_argument("--silencioso", action="store_true", help="sin traza de eventos")
    args = p.parse_args(argv)

    if args.listar or not args.fixture:
        print("Fixtures disponibles:")
        for n in listar_facturas():
            print(f"  {n}")
        return 0 if args.listar else 1

    print(f"Pipeline sobre '{args.fixture}' — esto llama a Gemini y cuesta.")
    print()
    expediente, metricas = asyncio.run(correr(args.fixture, verbose=not args.silencioso))
    imprimir_resumen(expediente, metricas)

    if expediente and args.json:
        DIR_SALIDAS.mkdir(exist_ok=True)
        destino = DIR_SALIDAS / f"{args.fixture}.expediente.json"
        destino.write_text(
            json.dumps(expediente, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nExpediente guardado en {destino}")

    if expediente and args.memo:
        print()
        print(render_memo_markdown(expediente))

    if expediente is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
