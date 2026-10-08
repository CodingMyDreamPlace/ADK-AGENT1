"""Tabla determinista senal -> recomendacion. CERO LLM.

Esto es "ante una observacion, proponer acciones" aplicado al sistema mismo.
Un log que dice `tool_error en validador_proveedor_contrato: DatoNoDisponible`
obliga a alguien a interpretarlo. Una `RecomendacionOps` dice que hacer, quien
lo hace, con que urgencia y en que runbook.

Es una tabla y no un agente porque esta en el camino caliente de cada
invocacion: si cada error disparara una llamada al modelo, el sistema de
observabilidad costaria mas que el observado y se caeria justo durante una
tormenta de errores, que es cuando mas se lo necesita. Ademas, el free tier
tiene 20 requests/dia por modelo: gastarlos en explicar errores seria absurdo.

Las plantillas interpolan `{componente}` y `{detalle}` para que la accion sea
concreta ("revisar el fixture de validador_proveedor_contrato") y no generica.
"""

from __future__ import annotations

from ..esquemas import RecomendacionOps, SenalOps, Severidad

RUNBOOK_BASE = "docs/runbooks/"

#: tipo de senal -> (accion, tipo_accion, responsable, urgencia, runbook)
PLANTILLAS: dict[str, tuple[str, str, str, str, str]] = {
    "error_herramienta": (
        "Revisar la fuente de datos de {componente}: la herramienta fallo y el "
        "validador debe haber respondido no_evaluable. Verificar que los maestros "
        "(proveedores, OC, recepciones) existan y sean JSON valido.",
        "revisar_fixture",
        "ingenieria_agentes",
        "alta",
        "error-herramienta.md",
    ),
    "tormenta_reintentos": (
        "Investigar por que {componente} supera 2 reintentos: reintentar mas no "
        "ayuda si la causa es cuota o un dato corrupto. Revisar el perfil de "
        "cuota (AP_OPS_PERFIL_CUOTA) y el estado del proveedor del modelo.",
        "abrir_incidente",
        "plataforma",
        "alta",
        "tormenta-reintentos.md",
    ),
    "latencia_alta": (
        "{componente} excedio su presupuesto de latencia. Si es recurrente, "
        "subir el timeout del nodo o degradar a un modelo mas rapido.",
        "subir_timeout",
        "ingenieria_agentes",
        "media",
        "latencia-alta.md",
    ),
    "gasto_tokens_alto": (
        "La factura supero el presupuesto de tokens. Revisar el tamano de los "
        "esquemas de salida (cada validador reenvia su JSON Schema en cada "
        "llamada) y considerar degradar el modelo de los validadores.",
        "degradar_modelo",
        "ingenieria_agentes",
        "media",
        "gasto-tokens.md",
    ),
    "desacuerdo_validadores": (
        "El puntaje del modelo y el determinista discrepan en {componente}. No "
        "es un empate a promediar: un analista debe revisar esta factura y "
        "confirmar cual estimador tiene razon.",
        "marcar_para_revision_humana",
        "negocio_cxp",
        "alta",
        "desacuerdo-estimadores.md",
    ),
    "validador_no_evaluable": (
        "{componente} no pudo concluir, asi que la factura no puede resolver "
        "touchless. Resolver el insumo faltante (OCR, maestro, OC) y reprocesar.",
        "revisar_fixture",
        "negocio_cxp",
        "media",
        "validador-no-evaluable.md",
    ),
    "decision_baja_confianza": (
        "El modelo declaro baja confianza en {componente}. Enviar la factura a "
        "revision humana en lugar de confiar en el puntaje.",
        "marcar_para_revision_humana",
        "negocio_cxp",
        "media",
        "baja-confianza.md",
    ),
    "error_modelo": (
        "El modelo fallo en {componente}. Si es 429, revisar cuota diaria y por "
        "minuto del proveedor; si es 503, es transitorio y el retry deberia "
        "absorberlo. Si persiste, abrir incidente.",
        "reintentar",
        "plataforma",
        "alta",
        "error-modelo.md",
    ),
    "esquema_invalido": (
        "La salida de {componente} no valido contra su esquema. Revisar el "
        "prompt: probablemente pide un campo que el esquema no tiene o viceversa.",
        "revisar_prompt",
        "ingenieria_agentes",
        "alta",
        "esquema-invalido.md",
    ),
    "ciclo_critico_agotado": (
        "El critico no aprobo el plan tras el maximo de vueltas. El expediente "
        "salio con el mejor plan disponible: un analista debe revisarlo.",
        "marcar_para_revision_humana",
        "negocio_cxp",
        "media",
        "ciclo-critico-agotado.md",
    ),
    "desconocida": (
        "Senal no catalogada en {componente}. Revisar el detalle y, si se "
        "repite, agregar una plantilla a ops_advisor.PLANTILLAS.",
        "abrir_incidente",
        "ingenieria_agentes",
        "baja",
        "senal-desconocida.md",
    ),
}


def recomendar(senal: SenalOps) -> RecomendacionOps:
    """Devuelve la recomendacion para una senal. Total: nunca levanta.

    Una senal de tipo desconocido cae en la plantilla 'desconocida' en lugar de
    explotar: el sistema de observabilidad no puede ser una nueva fuente de
    fallas.
    """
    accion, tipo_accion, responsable, urgencia, runbook = PLANTILLAS.get(
        senal.tipo, PLANTILLAS["desconocida"]
    )

    # Una senal critica sube la urgencia aunque la plantilla diga otra cosa:
    # la severidad la fija quien detecta, la plantilla solo da el default.
    if senal.severidad in (Severidad.CRITICA, Severidad.ALTA) and urgencia != "alta":
        urgencia = "alta"

    return RecomendacionOps(
        senal=senal,
        accion_recomendada=accion.format(componente=senal.componente, detalle=senal.detalle),
        tipo_accion=tipo_accion,  # type: ignore[arg-type]
        responsable=responsable,  # type: ignore[arg-type]
        urgencia=urgencia,  # type: ignore[arg-type]
        evidencia=senal.detalle,
        runbook=RUNBOOK_BASE + runbook,
    )
