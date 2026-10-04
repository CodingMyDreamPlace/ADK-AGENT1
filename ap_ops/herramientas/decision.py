"""La compuerta de decision. EL UNICO LUGAR QUE DECIDE SI EL DINERO SE MUEVE.

Deliberadamente deterministica, y deliberadamente una tabla de reglas numeradas
en lugar de una formula o de un modelo. Tres razones, en orden de importancia:

1. **Reproducibilidad.** La misma factura tiene que producir la misma decision
   hoy y en seis meses. Un LLM con temperature=0 se acerca, pero no lo garantiza
   entre versiones del modelo.

2. **Auditabilidad.** `DecisionAP.regla_aplicada` devuelve 'R-03'. Un auditor
   lee la regla R-03 en este archivo y reconstruye el razonamiento completo sin
   leer un solo prompt ni un log de modelo. Eso es lo que hace defendible al
   sistema ante una revision.

3. **Responsabilidad.** Si el sistema retiene mal una factura, la causa es una
   regla que alguien escribio y que se puede cambiar. Si la hubiera decidido un
   modelo, la causa seria "el modelo juzgo", que no es una respuesta.

Las reglas se evaluan EN ORDEN y la primera que coincide gana. El orden NO es
arbitrario y es la parte mas discutible del sistema, asi que queda explicito:

    R-01 primero  porque no se aprueba lo que no se pudo verificar.
    R-02 antes de R-03  porque una sospecha de fraude se INVESTIGA, no se
         rechaza. La factura puede ser legitima y el proveedor estar esperando
         su pago; rechazarla lo castiga por un ataque que sufrio la empresa, y
         cierra el caso sin averiguar si hay un patron mas amplio.
    R-03 (rechazo) solo para error material confirmado, sin duda razonable.
"""

from __future__ import annotations

from .. import config
from ..esquemas import Severidad
from . import catalogo
from ._dinero import dec, flt

#: Descripcion de cada regla, para el memo y para la auditoria.
REGLAS: dict[str, str] = {
    "R-01": "Datos no verificables: algun validador no pudo concluir",
    "R-02": "Sospecha de fraude: retener y escalar a cumplimiento para investigar",
    "R-03": "Error material confirmado: pagar seria un doble pago o un pago invalido",
    "R-04": "Hallazgo bloqueante: requiere subsanacion antes de liberar el pago",
    "R-05": "Puntaje de riesgo en o por encima del umbral de retencion",
    "R-06": "Puntaje de riesgo por encima del umbral touchless",
    "R-07": "Factura limpia pero por encima del umbral de monto",
    "R-08": "Factura conforme y por debajo del umbral de monto",
}


def _sev(valor) -> Severidad:
    if isinstance(valor, Severidad):
        return valor
    try:
        return Severidad(str(valor))
    except ValueError:
        return Severidad.MEDIA


def _peso(valor) -> int:
    return config.PESO_SEVERIDAD.get(_sev(valor).value, 0)


def aplicar_compuerta(
    puntaje_riesgo: int,
    hallazgos: list[dict],
    monto_total: float,
    validadores_no_evaluables: list[str] | None = None,
) -> dict:
    """Decide el destino de la factura. Primera regla que coincide, gana.

    Args:
      puntaje_riesgo: 0-100. En el pipeline real es el del modelo; en el camino
        sin LLM es `puntaje_determinista`.
      hallazgos: hallazgos ya consolidados y deduplicados.
      monto_total: total de la factura, en su moneda.
      validadores_no_evaluables: nombres de los validadores que no concluyeron.

    Returns:
      Un dict con la forma de `DecisionAP`.
    """
    no_evaluables = list(validadores_no_evaluables or [])
    bloqueantes = [h for h in hallazgos if h.get("bloquea_pago")]
    codigos_bloq = [h.get("codigo", "") for h in bloqueantes]
    investigacion = [
        h for h in hallazgos if catalogo.requiere_investigacion(h.get("codigo", ""))
    ]
    rechazo = [h for h in hallazgos if catalogo.justifica_rechazo(h.get("codigo", ""))]
    monto = flt(dec(monto_total))

    # ---------------------------------------------------------------- R-01
    if no_evaluables:
        return _decision(
            "retener",
            "R-01",
            f"No se pudo verificar: {', '.join(no_evaluables)}. "
            f"No se aprueba lo que no se verifico.",
            umbral=0.0,
            monto_autorizado=0.0,
            aprobador="analista_cxp",
            confirmacion=True,
            condiciones=[
                f"Resolver el insumo faltante de {v} y reprocesar" for v in no_evaluables
            ],
        )

    # ---------------------------------------------------------------- R-02
    # Antes del rechazo a proposito: una sospecha de fraude se investiga.
    if investigacion:
        return _decision(
            "retener",
            "R-02",
            f"Sospecha de fraude: {', '.join(h['codigo'] for h in investigacion)}. "
            f"La factura puede ser legitima y el beneficiario haber sido "
            f"sustituido por un tercero. Verificar por canal independiente "
            f"antes de cualquier pago, y revisar si hay un patron mas amplio.",
            umbral=0.0,
            monto_autorizado=0.0,
            aprobador="cumplimiento",
            confirmacion=True,
            condiciones=[
                "Verificar la cuenta de abono llamando al contacto REGISTRADO del "
                "proveedor, no al que figura en el correo que trajo la factura",
                "Obtener confirmacion escrita del cambio de cuenta, si fuera legitimo",
                "Revisar otras facturas del mismo proveedor en los ultimos 90 dias",
            ],
        )

    # ---------------------------------------------------------------- R-03
    if rechazo:
        return _decision(
            "rechazar",
            "R-03",
            f"Error material confirmado: {', '.join(h['codigo'] for h in rechazo)}. "
            f"No queda duda razonable: pagar seria un doble pago o un pago "
            f"contra un comprobante invalido.",
            umbral=0.0,
            monto_autorizado=0.0,
            aprobador="jefe_cxp",
            confirmacion=True,
        )

    # ---------------------------------------------------------------- R-04
    if bloqueantes:
        return _decision(
            "retener",
            "R-04",
            f"Hallazgo bloqueante: {', '.join(codigos_bloq)}. "
            f"Pagable una vez subsanado.",
            umbral=0.0,
            monto_autorizado=0.0,
            aprobador="jefe_cxp" if monto > config.UMBRAL_MONTO_HITL else "analista_cxp",
            confirmacion=True,
            condiciones=[f"Subsanar {c} antes de liberar el pago" for c in codigos_bloq],
        )

    # ---------------------------------------------------------------- R-05
    if puntaje_riesgo >= config.UMBRAL_RETENER:
        return _decision(
            "retener",
            "R-05",
            f"Puntaje de riesgo {puntaje_riesgo} alcanza el umbral de retencion "
            f"({config.UMBRAL_RETENER}).",
            umbral=float(config.UMBRAL_RETENER),
            monto_autorizado=0.0,
            aprobador="jefe_cxp",
            confirmacion=True,
        )

    # ---------------------------------------------------------------- R-06
    if puntaje_riesgo > config.UMBRAL_TOUCHLESS:
        return _decision(
            "aprobar_con_condiciones",
            "R-06",
            f"Puntaje de riesgo {puntaje_riesgo} supera el umbral touchless "
            f"({config.UMBRAL_TOUCHLESS}) pero no alcanza el de retencion "
            f"({config.UMBRAL_RETENER}). Pagable sujeto a condiciones.",
            umbral=float(config.UMBRAL_TOUCHLESS),
            monto_autorizado=monto,
            aprobador="analista_cxp" if monto <= config.UMBRAL_MONTO_HITL else "jefe_cxp",
            confirmacion=monto > config.UMBRAL_MONTO_HITL,
            condiciones=[
                f"Ejecutar la accion propuesta para {h.get('codigo', '?')}"
                for h in hallazgos
            ],
        )

    # ---------------------------------------------------------------- R-07
    if monto > config.UMBRAL_MONTO_HITL:
        return _decision(
            "aprobar_con_condiciones",
            "R-07",
            f"Sin hallazgos relevantes (puntaje {puntaje_riesgo}), pero el monto "
            f"{monto:,.2f} supera el umbral de confirmacion humana "
            f"({config.UMBRAL_MONTO_HITL:,.2f}).",
            umbral=config.UMBRAL_MONTO_HITL,
            monto_autorizado=monto,
            aprobador="jefe_cxp",
            confirmacion=True,
            condiciones=["Confirmacion de jefatura por monto"],
        )

    # ---------------------------------------------------------------- R-08
    return _decision(
        "touchless_approve",
        "R-08",
        f"Factura conforme: puntaje {puntaje_riesgo} por debajo del umbral "
        f"touchless ({config.UMBRAL_TOUCHLESS}) y monto {monto:,.2f} por debajo "
        f"del umbral de confirmacion ({config.UMBRAL_MONTO_HITL:,.2f}).",
        umbral=float(config.UMBRAL_TOUCHLESS),
        monto_autorizado=monto,
        aprobador="analista_cxp",
        confirmacion=False,
    )


def _decision(
    resultado: str,
    regla: str,
    motivo: str,
    umbral: float,
    monto_autorizado: float,
    aprobador: str,
    confirmacion: bool,
    condiciones: list[str] | None = None,
) -> dict:
    """Arma el dict con forma de DecisionAP.

    Nota sobre `monto_autorizado`: en v0.1 es el total completo para las
    aprobaciones y 0 para las retenciones. La autorizacion PARCIAL (pagar el
    monto no disputado y retener el resto) queda fuera de alcance a proposito:
    requiere una decision de politica del area, no una formula.
    """
    return {
        "resultado": resultado,
        "motivo": motivo,
        "condiciones": condiciones or [],
        "monto_autorizado": monto_autorizado,
        "requiere_confirmacion_humana": confirmacion,
        "regla_aplicada": regla,
        "umbral_usado": umbral,
        "aprobador_sugerido": aprobador,
    }
