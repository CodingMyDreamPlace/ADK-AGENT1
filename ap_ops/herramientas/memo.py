"""Render del memo de auditoria en Markdown. Sin LLM.

Es trabajo de plantilla: los textos ya los produjeron las herramientas (la
evidencia) y los agentes (los titulos y las acciones). Generarlo con un modelo
costaria una llamada mas y abriria la puerta a que el memo diga algo distinto de
lo que dice el expediente, que es justo lo que un documento de auditoria no
puede hacer.

El memo es el artifact que un humano lee. Por eso arranca con la decision y la
regla aplicada: lo primero que alguien quiere saber es que se resolvio y por que.
"""

from __future__ import annotations

from ..esquemas import NOMBRE_COMPROBANTE, Severidad
from . import catalogo
from .decision import REGLAS

ICONO_SEVERIDAD: dict[str, str] = {
    "critica": "[CRITICA]",
    "alta": "[ALTA]   ",
    "media": "[MEDIA]  ",
    "baja": "[BAJA]   ",
    "informativa": "[INFO]   ",
}

ETIQUETA_DECISION: dict[str, str] = {
    "touchless_approve": "APROBADA SIN INTERVENCION",
    "aprobar_con_condiciones": "APROBADA CON CONDICIONES",
    "retener": "RETENIDA",
    "rechazar": "RECHAZADA",
}


def render_memo_markdown(expediente: dict) -> str:
    """Construye el memo de auditoria a partir de un ExpedienteAP serializado.

    Args:
      expediente: dict con la forma de `ExpedienteAP`.

    Returns:
      El memo en Markdown, listo para guardar como artifact.
    """
    f = expediente.get("factura", {})
    dec_ = expediente.get("decision", {})
    cons = expediente.get("consolidado", {})
    ev = expediente.get("evaluacion", {})
    plan = expediente.get("plan_accion", {})

    moneda = f.get("moneda", "PEN")
    total = float(f.get("total", 0.0))
    resultado = dec_.get("resultado", "?")
    regla = dec_.get("regla_aplicada", "?")

    L: list[str] = []
    ap = L.append

    # ------------------------------------------------------------- encabezado
    ap(f"# Memo de auditoria — {f.get('id_factura', '?')}")
    ap("")
    ap(f"**Decision: {ETIQUETA_DECISION.get(resultado, resultado.upper())}**")
    ap("")
    ap(f"- **Regla aplicada:** `{regla}` — {REGLAS.get(regla, 'sin descripcion')}")
    ap(f"- **Umbral comparado:** {dec_.get('umbral_usado', 0):,.2f}")
    ap(f"- **Motivo:** {dec_.get('motivo', '-')}")
    ap(
        f"- **Puntaje de riesgo:** {ev.get('puntaje_riesgo', '-')}/100 "
        f"(determinista: {cons.get('puntaje_determinista', '-')}/100)"
    )
    if ev.get("desacuerdo_con_determinista"):
        ap(
            f"- **Desacuerdo entre estimadores:** si — "
            f"{ev.get('justificacion_desacuerdo') or 'sin justificar'}"
        )
    ap(f"- **Monto autorizado:** {moneda} {dec_.get('monto_autorizado', 0):,.2f}")
    ap(
        f"- **Confirmacion humana:** "
        f"{'requerida' if dec_.get('requiere_confirmacion_humana') else 'no requerida'}"
    )
    ap(f"- **Aprobador sugerido:** {dec_.get('aprobador_sugerido', '-')}")
    ap("")

    # ------------------------------------------------------------- comprobante
    ap("## Comprobante")
    ap("")
    tipo = f.get("tipo_comprobante", "?")
    ap(f"| | |")
    ap(f"|---|---|")
    ap(f"| Proveedor | {f.get('razon_social', '?')} |")
    ap(f"| RUC | {f.get('ruc_proveedor', '?')} |")
    ap(f"| Comprobante | {NOMBRE_COMPROBANTE.get(tipo, tipo)} {f.get('serie', '')}-{f.get('numero', '')} |")
    ap(f"| Fecha de emision | {f.get('fecha_emision', '?')} |")
    ap(f"| Orden de compra | {f.get('orden_compra') or 'sin orden de compra'} |")
    ap(f"| Subtotal | {moneda} {float(f.get('subtotal', 0)):,.2f} |")
    ap(f"| IGV | {moneda} {float(f.get('igv', 0)):,.2f} |")
    ap(f"| **Total** | **{moneda} {total:,.2f}** |")
    ap(f"| Condicion de pago | {f.get('condicion_pago', '?')} |")
    ap("")

    # ------------------------------------------------------------- hallazgos
    hallazgos = cons.get("hallazgos", [])
    ap(f"## Hallazgos ({len(hallazgos)})")
    ap("")
    if not hallazgos:
        ap("Sin hallazgos. Los cuatro validadores resolvieron conforme.")
        ap("")
    else:
        for h in hallazgos:
            sev = str(h.get("severidad", "media"))
            icono = ICONO_SEVERIDAD.get(sev, "[?]")
            bloq = " **BLOQUEA EL PAGO**" if h.get("bloquea_pago") else ""
            ap(f"### `{h.get('codigo', '?')}` {icono} {h.get('titulo', '')}{bloq}")
            ap("")
            ap(f"- **Campo:** `{h.get('campo_afectado', '-')}`")
            declarado, esperado = h.get("valor_declarado"), h.get("valor_esperado")
            if declarado is not None and esperado is not None:
                ap(f"- **Declarado:** {declarado} — **esperado:** {esperado}")
            elif declarado is not None:
                ap(f"- **Declarado:** {declarado}")
            ap(f"- **Monto impactado:** {moneda} {float(h.get('monto_impactado', 0)):,.2f}")
            ap(f"- **Fuente:** {h.get('fuente', '-')} (confianza {h.get('confianza', '-')})")
            ap(f"- **Evidencia:** {h.get('evidencia', '-')}")
            ap("")

    no_eval = cons.get("validadores_no_evaluables", [])
    if no_eval:
        ap("### Validadores que no pudieron concluir")
        ap("")
        for v in no_eval:
            ap(f"- `{v}`")
        ap("")
        ap(
            "> Una factura con validadores no evaluables no puede resolver "
            "*touchless*: no se aprueba lo que no se verifico."
        )
        ap("")

    # ------------------------------------------------------------- acciones
    acciones = plan.get("acciones", [])
    ap(f"## Acciones propuestas ({len(acciones)})")
    ap("")
    if not acciones:
        ap("Sin acciones: no hay hallazgos que resolver.")
        ap("")
    else:
        ap("| # | Accion | Responsable | SLA | Hallazgo | Bloquea |")
        ap("|---|---|---|---|---|---|")
        for a in sorted(acciones, key=lambda x: x.get("prioridad", 99)):
            ap(
                f"| {a.get('prioridad', '-')} "
                f"| {a.get('accion', '-')} "
                f"| {a.get('responsable', '-')} "
                f"| {a.get('sla_horas', '-')} h "
                f"| `{a.get('codigo_hallazgo', '-')}` "
                f"| {'si' if a.get('bloquea_pago') else 'no'} |"
            )
        ap("")
        for a in sorted(acciones, key=lambda x: x.get("prioridad", 99)):
            ap(f"**{a.get('accion', '-')}**")
            ap("")
            ap(f"- Justificacion: {a.get('justificacion', '-')}")
            ap(f"- Si no se actua: {a.get('impacto_si_no_se_actua', '-')}")
            ap(
                f"- Reversible: {'si' if a.get('reversible') else 'NO'} "
                f"| Requiere aprobacion humana: "
                f"{'si' if a.get('requiere_aprobacion_humana') else 'no'}"
            )
            ap("")

    # --------------------------------------------- invariante hallazgo/accion
    huerfanos = plan.get("hallazgos_sin_accion", [])
    sin_respaldo = plan.get("acciones_sin_hallazgo", [])
    if huerfanos or sin_respaldo:
        ap("## Control de integridad del plan")
        ap("")
        if huerfanos:
            ap(f"- **Hallazgos sin accion propuesta:** {', '.join(huerfanos)}")
        if sin_respaldo:
            ap(f"- **Acciones sin hallazgo que las respalde:** {', '.join(sin_respaldo)}")
        ap("")
        ap("> Ambas listas deberian estar vacias. Revisar el plan de accion.")
        ap("")

    # ------------------------------------------------------------- condiciones
    # El titulo depende del resultado: lo que condiciona una aprobacion es
    # distinto de lo que hay que resolver para levantar una retencion, y leer
    # "Condiciones de la aprobacion" en una factura retenida confunde a quien
    # tiene que actuar.
    condiciones = dec_.get("condiciones", [])
    if condiciones:
        titulo_cond = {
            "touchless_approve": "Condiciones de la aprobacion",
            "aprobar_con_condiciones": "Condiciones de la aprobacion",
            "retener": "Requisitos para levantar la retencion",
            "rechazar": "Pasos de cierre del rechazo",
        }.get(resultado, "Condiciones")
        ap(f"## {titulo_cond}")
        ap("")
        for c in condiciones:
            ap(f"- {c}")
        ap("")

    # ------------------------------------------------------------- pie
    ap("---")
    ap("")
    ap(
        f"Expediente `{expediente.get('id_expediente', '?')}` · "
        f"pipeline v{expediente.get('version_pipeline', '?')} · "
        f"generado {expediente.get('generado_en', '?')}"
    )
    ap(
        f"Costo: {expediente.get('llamadas_llm', 0)} llamada(s) al modelo, "
        f"{expediente.get('tokens_consumidos', 0):,} tokens · "
        f"iteraciones del critico: {expediente.get('iteraciones_critico', 0)}"
    )

    return "\n".join(L) + "\n"


def resumen_una_linea(expediente: dict) -> str:
    """Una linea para logs y para la cola de trabajo."""
    f = expediente.get("factura", {})
    d = expediente.get("decision", {})
    cons = expediente.get("consolidado", {})
    codigos = [h.get("codigo", "") for h in cons.get("hallazgos", [])]
    sev = str(cons.get("severidad_maxima", "informativa"))
    return (
        f"{f.get('id_factura', '?')} | {f.get('moneda', 'PEN')} "
        f"{float(f.get('total', 0)):>12,.2f} | {d.get('resultado', '?'):<24} "
        f"| {d.get('regla_aplicada', '?')} | riesgo "
        f"{expediente.get('evaluacion', {}).get('puntaje_riesgo', '-'):>3}/100 "
        f"| sev {sev:<11} | {', '.join(codigos) or 'sin hallazgos'}"
    )


def catalogo_markdown() -> str:
    """Tabla de referencia del catalogo de codigos. Para documentacion."""
    L = ["# Catalogo de codigos de hallazgo", "", "| Codigo | Titulo | Categoria | Severidad base | Bloquea pago |", "|---|---|---|---|---|"]
    for codigo in sorted(catalogo.CATALOGO):
        titulo, cat = catalogo.CATALOGO[codigo]
        sev: Severidad = catalogo.severidad_base(codigo)
        bloq = "si" if catalogo.bloquea_pago(codigo) else "no"
        L.append(f"| `{codigo}` | {titulo} | {cat} | {sev.value} | {bloq} |")
    return "\n".join(L) + "\n"
