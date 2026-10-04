"""Smoke test del camino determinista completo. CERO llamadas a LLM, CERO costo.

Corre las 7 facturas sinteticas por las 4 herramientas, consolida, aplica la
compuerta de decision y renderiza el memo. Es el pipeline entero menos los
agentes, y por eso es la red de seguridad mas barata del proyecto: si algo se
rompe aca, se rompe antes de gastar un token.

    python -m ap_ops.scripts.smoke_esquemas
    python -m ap_ops.scripts.smoke_esquemas --memo factura_fraude_cuenta
    python -m ap_ops.scripts.smoke_esquemas --catalogo
"""

from __future__ import annotations

import argparse
import os
import sys

# Fecha fija: sin esto, `factura_fraude_cuenta` deja de ser "proveedor nuevo"
# cuando pasen 90 dias y el smoke empieza a dar resultados distintos solo por
# el paso del tiempo.
os.environ.setdefault("AP_OPS_FECHA_REFERENCIA", "2026-10-03")

from .. import config  # noqa: E402
from ..esquemas import ConsolidadoValidacion, DecisionAP  # noqa: E402
from ..herramientas import (  # noqa: E402
    analizar_duplicados_fraude,
    analizar_proveedor_contrato,
    analizar_tres_vias,
    analizar_tributario,
    aplicar_compuerta,
    cargar_factura,
    catalogo_markdown,
    consolidar,
    hallazgo_desde_desviacion,
    listar_facturas,
    render_memo_markdown,
)
from ..herramientas._fechas import ahora_iso  # noqa: E402

VALIDADORES = {
    "tres_vias": analizar_tres_vias,
    "tributario": analizar_tributario,
    "duplicados_fraude": analizar_duplicados_fraude,
    "proveedor_contrato": analizar_proveedor_contrato,
}


def procesar(nombre: str) -> dict:
    """Corre el camino determinista completo sobre una factura.

    Devuelve un expediente PARCIAL: tiene factura, consolidado y decision, pero
    no `evaluacion` ni `plan_accion`, que son salidas de agentes. En Fase 2 el
    grafo completa esas dos piezas.
    """
    factura = cargar_factura(nombre)

    hallazgos: list[dict] = []
    no_evaluables: list[str] = []
    resultados: dict[str, dict] = {}

    for clave, fn in VALIDADORES.items():
        r = fn(nombre)
        resultados[clave] = r

        if not r.get("datos_disponibles"):
            # Falla de infraestructura: el validador no puede concluir.
            no_evaluables.append(clave)
            continue
        if r.get("confianza_insuficiente"):
            no_evaluables.append(clave)

        for desv in r.get("desviaciones", []):
            hallazgos.append(hallazgo_desde_desviacion(desv))

    cons = consolidar(factura.id_factura, hallazgos, no_evaluables)
    puntaje = cons["puntaje_determinista"]

    dec_ = aplicar_compuerta(
        puntaje_riesgo=puntaje,
        hallazgos=cons["hallazgos"],
        monto_total=factura.total,
        validadores_no_evaluables=cons["validadores_no_evaluables"],
    )

    # Validacion de esquemas: si el dict no cuadra con el modelo Pydantic, el
    # smoke tiene que fallar aqui y no en Fase 2 dentro del grafo.
    ConsolidadoValidacion.model_validate(
        {k: v for k, v in cons.items() if k not in ("ruta", "codigos", "codigos_bloqueantes")}
    )
    DecisionAP.model_validate(dec_)

    return {
        "nombre_fixture": nombre,
        "id_expediente": f"EXP-{factura.id_factura}",
        "id_factura": factura.id_factura,
        "version_pipeline": config.VERSION_PIPELINE,
        "generado_en": ahora_iso(),
        "factura": factura.model_dump(),
        "resultados_crudos": resultados,
        "consolidado": cons,
        "evaluacion": {
            "puntaje_riesgo": puntaje,
            "puntaje_determinista": puntaje,
            "desacuerdo_con_determinista": False,
        },
        "plan_accion": {"acciones": [], "hallazgos_sin_accion": [], "acciones_sin_hallazgo": []},
        "decision": dec_,
        "iteraciones_critico": 0,
        "llamadas_llm": 0,
        "tokens_consumidos": 0,
    }


def tabla(expedientes: list[dict]) -> None:
    print()
    print("=" * 118)
    print(
        f"{'fixture':<26} {'total':>12}  {'pts':>3}  {'sev':<11} "
        f"{'decision':<24} {'regla':<5} hallazgos"
    )
    print("=" * 118)
    for e in expedientes:
        f, c, d = e["factura"], e["consolidado"], e["decision"]
        codigos = ", ".join(c["codigos"]) or "-"
        ne = f"  [no evaluable: {','.join(c['validadores_no_evaluables'])}]" if c["validadores_no_evaluables"] else ""
        print(
            f"{e['nombre_fixture']:<26} {f['total']:>12,.2f}  "
            f"{c['puntaje_determinista']:>3}  {str(c['severidad_maxima']):<11} "
            f"{d['resultado']:<24} {d['regla_aplicada']:<5} {codigos}{ne}"
        )
    print("=" * 118)


def resumen(expedientes: list[dict]) -> int:
    """Imprime el resumen y devuelve el codigo de salida."""
    total = len(expedientes)
    touchless = sum(1 for e in expedientes if e["decision"]["resultado"] == "touchless_approve")
    retenidas = sum(1 for e in expedientes if e["decision"]["resultado"] in ("retener", "rechazar"))
    hallazgos = sum(len(e["consolidado"]["hallazgos"]) for e in expedientes)
    no_eval = sum(1 for e in expedientes if e["consolidado"]["validadores_no_evaluables"])

    print()
    print(f"  facturas procesadas    : {total}")
    print(f"  touchless              : {touchless} ({touchless / total:.0%})")
    print(f"  retenidas o rechazadas : {retenidas}")
    print(f"  con validador no evaluable: {no_eval}")
    print(f"  hallazgos totales      : {hallazgos}")
    print(f"  llamadas a LLM         : 0  <-- todo este camino es determinista")
    print()

    # Invariantes que el smoke debe garantizar.
    fallas: list[str] = []
    for e in expedientes:
        c, d = e["consolidado"], e["decision"]
        if d["resultado"] == "touchless_approve" and c["hallazgos"]:
            fallas.append(f"{e['nombre_fixture']}: touchless con hallazgos")
        if d["resultado"] == "touchless_approve" and c["validadores_no_evaluables"]:
            fallas.append(f"{e['nombre_fixture']}: touchless con validador no evaluable")
        if c["codigos_bloqueantes"] and d["resultado"] in ("touchless_approve", "aprobar_con_condiciones"):
            fallas.append(f"{e['nombre_fixture']}: aprobada con hallazgo bloqueante")
        if d["regla_aplicada"] not in ("R-01", "R-02", "R-03", "R-04", "R-05", "R-06", "R-07", "R-08"):
            fallas.append(f"{e['nombre_fixture']}: regla desconocida {d['regla_aplicada']}")

    if fallas:
        print("  INVARIANTES VIOLADOS:")
        for f in fallas:
            print(f"    - {f}")
        print()
        return 1

    print("  invariantes OK: ninguna factura aprobada con hallazgo bloqueante,")
    print("                  ninguna touchless con hallazgos o datos no verificados.")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Smoke test determinista de AP Ops.")
    p.add_argument("--memo", metavar="FIXTURE", help="imprime el memo de una factura")
    p.add_argument("--catalogo", action="store_true", help="imprime el catalogo de codigos")
    args = p.parse_args(argv)

    if args.catalogo:
        print(catalogo_markdown())
        return 0

    if args.memo:
        print(render_memo_markdown(procesar(args.memo)))
        return 0

    expedientes = [procesar(n) for n in listar_facturas()]
    tabla(expedientes)
    return resumen(expedientes)


if __name__ == "__main__":
    sys.exit(main())
