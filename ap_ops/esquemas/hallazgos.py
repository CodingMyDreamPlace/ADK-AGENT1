"""El hallazgo: la unidad atomica de todo el sistema.

`Hallazgo` es el tipo mas importante del paquete. Todo lo que sigue en el
pipeline (el score, el plan de accion, la decision, el memo) se deriva de una
lista de hallazgos. Y el invariante central del diseno se expresa sobre el:

    todo Hallazgo.codigo debe aparecer en algun AccionPropuesta.codigo_hallazgo

Es decir: "ante una observacion, proponer acciones" no es una aspiracion del
prompt, es una propiedad verificable sobre estos dos modelos.

A diferencia de los esquemas de `factura.py`, estos modelos NO usan
`extra="forbid"`. Son salidas de un LLM: si el modelo agrega un campo de mas,
preferimos ignorarlo a abortar la invocacion y pagar un reintento.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .comunes import Categoria, DatoVerificado, Severidad


class Hallazgo(BaseModel):
    """Una desviacion detectada en la factura.

    El `codigo` sigue el formato PREFIJO-TEMA-NNN con un prefijo por validador:
    TRV (tres vias), TRI (tributario), FRA (duplicado/fraude), PRV (proveedor/
    contrato), INT (integridad de datos). Es un codigo cerrado y estable, no
    texto libre, por tres razones:

      1. Permite assertions exactas en los tests de expedientes dorados
         (comparar conjuntos de codigos, no parrafos).
      2. Permite agrupar metricas y armar runbooks por codigo.
      3. Es el campo por el que `AccionPropuesta` se vincula al hallazgo.
    """

    codigo: str = Field(
        description="Codigo estable del catalogo, p.ej. 'TRI-IGV-001', 'FRA-CTA-003'."
    )
    titulo: str = Field(description="Una linea, en espanol, legible por un analista.")
    severidad: Severidad
    categoria: Categoria
    campo_afectado: str = Field(
        description="Ruta exacta al campo, p.ej. 'igv', 'cuenta_bancaria.cci', "
        "'lineas[2].precio_unitario'. Sin esto el analista no sabe donde mirar."
    )
    valor_declarado: str | None = Field(
        default=None, description="Lo que dice la factura."
    )
    valor_esperado: str | None = Field(
        default=None, description="Lo que deberia decir segun la herramienta."
    )
    evidencia: str = Field(
        description="Cita LITERAL del calculo o lookup determinista que lo probo. "
        "Es lo que un auditor va a releer, asi que no admite parafraseo."
    )
    fuente: Literal["herramienta", "modelo"] = Field(
        default="herramienta",
        description="Un hallazgo con fuente='modelo' no tiene respaldo "
        "determinista: se puede reportar, pero nunca debe bloquear un pago solo.",
    )
    confianza: float = Field(ge=0.0, le=1.0)
    monto_impactado: float = Field(
        default=0.0,
        description="Cuanto dinero esta en juego. Es lo que hace la severidad "
        "proporcional en vez de arbitraria, y lo que ordena la cola del analista.",
    )
    bloquea_pago: bool = Field(
        description="Si es True, ninguna regla puede resolver en touchless_approve."
    )


class ConsolidadoValidacion(BaseModel):
    """Salida de `nodo_consolidar`: determinista, SIN LLM.

    Este nodo existe por dos razones que vale la pena separar:

      1. Deduplica. Dos validadores distintos pueden reportar el mismo problema
         (una factura sin OC dispara TRV y PRV). Sin dedup, el score se infla.

      2. Produce `puntaje_determinista`, que es el control cruzado contra el
         score del LLM. Si el modelo dice 15 y la tabla dice 70, eso no es un
         empate que haya que promediar: es una senal de observabilidad
         (`desacuerdo_validadores`) que dispara revision humana.

    Tambien es el nodo que decide la ruta: sin hallazgos -> atajo touchless,
    que se saltea scoring, plan y critico y ahorra 3 llamadas al modelo.
    """

    id_factura: str
    hallazgos: list[Hallazgo] = Field(default_factory=list)
    puntaje_determinista: int = Field(ge=0, le=100)
    severidad_maxima: Severidad
    monto_total_impactado: float = 0.0
    validadores_no_evaluables: list[str] = Field(
        default_factory=list,
        description="Validadores que no pudieron concluir. Un pipeline con "
        "validadores no evaluables NUNCA puede resolver touchless: no sabemos "
        "lo que no verificamos.",
    )
    resumen_por_categoria: list[DatoVerificado] = Field(default_factory=list)


class EvaluacionRiesgo(BaseModel):
    """Salida de `agente_scoring_riesgo`. Aca si interviene el LLM.

    Lo que el modelo aporta y una tabla no puede: correlacion cruzada. Un
    proveedor nuevo es riesgo bajo. Un CCI que no coincide es riesgo medio. Un
    monto 0.5% debajo del umbral de aprobacion es riesgo bajo. Los tres juntos
    son un fraude en curso, y eso no sale de sumar los tres puntajes.

    Los campos `puntaje_determinista` / `desacuerdo_con_determinista` obligan al
    modelo a mirar el numero de la tabla y declarar explicitamente si se aparta.
    Es mas barato y mas confiable que inferir el desacuerdo por fuera.
    """

    id_factura: str
    puntaje_riesgo: int = Field(ge=0, le=100)
    severidad_global: Severidad
    hallazgos: list[Hallazgo] = Field(
        description="Los hallazgos consolidados, posiblemente con severidad "
        "reajustada por correlacion. El modelo NO puede inventar hallazgos nuevos."
    )
    codigos_bloqueantes: list[str] = Field(default_factory=list)
    resumen_ejecutivo: str = Field(description="Maximo 4 lineas, para el memo.")
    monto_en_riesgo: float = 0.0

    puntaje_determinista: int = Field(
        ge=0, le=100, description="Copiado del consolidado. Control cruzado."
    )
    desacuerdo_con_determinista: bool
    justificacion_desacuerdo: str | None = Field(
        default=None,
        description="Obligatorio si desacuerdo_con_determinista es True.",
    )
    confianza: float = Field(ge=0.0, le=1.0)
