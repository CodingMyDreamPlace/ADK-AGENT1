"""Eje de operaciones: "ante una observacion, proponer acciones" aplicado al sistema mismo.

Estos modelos son el espejo de `hallazgos.py` + `acciones.py`, pero un nivel
mas arriba: donde `Hallazgo`/`AccionPropuesta` describen que anda mal con una
FACTURA, `SenalOps`/`RecomendacionOps` describen que anda mal con el PIPELINE
que mira la factura, y que hacer al respecto.

La diferencia con logging convencional es el campo `accion_recomendada`. Un log
que dice "tool_error en validador_proveedor_contrato: FileNotFoundError" obliga
a alguien a interpretarlo. Una `RecomendacionOps` dice "revisar el fixture
maestro_proveedores.json; mientras tanto el validador marca no_evaluable y la
factura no puede resolver touchless", con responsable y runbook. Un log es un
dato; esto es un pedido de trabajo.

El mapeo senal -> recomendacion es una TABLA DETERMINISTA
(`plugins/ops_advisor.py`), no un agente. Esta en el camino caliente de cada
invocacion: si cada error de herramienta disparara una llamada a un modelo, el
sistema de observabilidad costaria mas que el sistema observado, y se caeria
justo cuando hay una tormenta de errores que es cuando mas se lo necesita.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .comunes import DatoVerificado, Severidad

TipoSenal = Literal[
    "error_herramienta",
    "tormenta_reintentos",
    "latencia_alta",
    "gasto_tokens_alto",
    "desacuerdo_validadores",
    "validador_no_evaluable",
    "decision_baja_confianza",
    "error_modelo",
    "esquema_invalido",
    "ciclo_critico_agotado",
    "desconocida",
]

#: Que hacer. Igual que `TipoAccion` en el eje de negocio: catalogo cerrado para
#: que sea enrutable y agregable en metricas.
TipoAccionOps = Literal[
    "reintentar",
    "degradar_modelo",
    "subir_timeout",
    "revisar_fixture",
    "abrir_incidente",
    "marcar_para_revision_humana",
    "ajustar_umbral",
    "cachear_resultado",
    "revisar_prompt",
    "ninguna",
]


class SenalOps(BaseModel):
    """Algo observable que paso durante una invocacion."""

    tipo: TipoSenal
    severidad: Severidad
    componente: str = Field(
        description="Donde paso: node_path, nombre de tool o de agente. "
        "Sin esto una senal no es accionable, solo ruido."
    )
    detalle: str
    metricas: list[DatoVerificado] = Field(
        default_factory=list,
        description="Los numeros que la respaldan (latencia medida vs presupuesto, "
        "tokens, numero de intento).",
    )
    invocation_id: str
    id_factura: str | None = Field(
        default=None,
        description="Correlaciona la senal tecnica con el caso de negocio. Es lo "
        "que permite responder 'que facturas se vieron afectadas por la "
        "degradacion de ayer'.",
    )


class RecomendacionOps(BaseModel):
    """Senal + que hacer al respecto. La unidad de salida del eje ops."""

    senal: SenalOps
    accion_recomendada: str = Field(description="En imperativo y concreta.")
    tipo_accion: TipoAccionOps
    responsable: Literal["ingenieria_agentes", "plataforma", "negocio_cxp"] = Field(
        description="Separar negocio_cxp de los dos tecnicos importa: un "
        "'desacuerdo_validadores' no lo arregla un ingeniero, lo revisa un "
        "analista de cuentas por pagar."
    )
    urgencia: Literal["baja", "media", "alta"]
    evidencia: str
    runbook: str = Field(description="Ruta o URL del procedimiento.")


class RegistroAuditoria(BaseModel):
    """Lo que `PluginAuditoriaAP` acumula por invocacion y guarda como artifact.

    Es, a la vez, la traza de auditoria del negocio y la telemetria tecnica.
    Mantenerlos en un solo objeto es deliberado: la pregunta que se hace en la
    practica no es "cuanto tardo el nodo X" ni "por que se retuvo la factura Y"
    por separado, sino "por que esta factura tardo 40 segundos y termino
    retenida". Ese cruce solo se puede responder si ambos lados comparten el
    `invocation_id` en el mismo registro.
    """

    invocation_id: str
    id_factura: str | None = None
    inicio: str
    fin: str | None = None
    duracion_s: float = 0.0

    llamadas_llm: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    llamadas_herramienta: int = 0
    errores_herramienta: int = 0
    reintentos: int = 0

    latencias_por_nodo: list[DatoVerificado] = Field(default_factory=list)
    recomendaciones_ops: list[RecomendacionOps] = Field(default_factory=list)

    decision_final: str | None = None
