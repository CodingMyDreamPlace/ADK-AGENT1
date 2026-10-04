"""Consolidacion y puntaje determinista. Sin LLM.

Este modulo hace tres cosas, y conviene separarlas porque se suelen confundir:

1. **Deduplica.** Dos validadores distintos pueden reportar el mismo problema
   desde angulos distintos. Sin dedup el puntaje se infla y el plan de accion
   propone dos veces lo mismo.

2. **Calcula `puntaje_determinista`.** Es el CONTROL CRUZADO del puntaje del
   modelo. No reemplaza al juicio del LLM —no sabe correlacionar— pero da un
   segundo estimador independiente. Dos estimadores que discrepan mucho son una
   senal, no un promedio a calcular.

3. **Decide la ruta del grafo.** Sin hallazgos, el pipeline saltea scoring, plan
   y critico: tres llamadas al modelo que no se gastan. Como la mayoria de las
   facturas estan limpias, este atajo es la mayor palanca de costo del sistema.
"""

from __future__ import annotations

from .. import config
from ..esquemas import DatoVerificado, Severidad, severidad_maxima
from . import catalogo
from ._dinero import dec, flt, redondear


def hallazgo_desde_desviacion(desv: dict, confianza: float = 1.0) -> dict:
    """Convierte una desviacion de herramienta en un Hallazgo con severidad base.

    Es el camino SIN LLM: lo usan el script de smoke y los tests dorados para
    ejercitar scoring, decision y memo sin gastar un token. En el pipeline real
    este paso lo hace el validador correspondiente, que ajusta la severidad al
    monto y al contexto en lugar de usar la base.
    """
    codigo = desv["codigo"]
    return {
        "codigo": codigo,
        "titulo": desv.get("titulo_sugerido") or catalogo.titulo(codigo),
        "severidad": catalogo.severidad_base(codigo).value,
        "categoria": desv.get("categoria") or catalogo.categoria(codigo),
        "campo_afectado": desv.get("campo_afectado", ""),
        "valor_declarado": desv.get("valor_declarado"),
        "valor_esperado": desv.get("valor_esperado"),
        "evidencia": desv.get("evidencia", ""),
        "fuente": "herramienta",
        "confianza": confianza,
        "monto_impactado": float(desv.get("monto_impactado", 0.0)),
        "bloquea_pago": catalogo.bloquea_pago(codigo),
    }


def _sev(h: dict) -> Severidad:
    """Severidad de un hallazgo en forma de dict, tolerante al tipo."""
    valor = h.get("severidad")
    if isinstance(valor, Severidad):
        return valor
    try:
        return Severidad(str(valor))
    except ValueError:
        return Severidad.MEDIA


def deduplicar(hallazgos: list[dict]) -> list[dict]:
    """Deja un hallazgo por codigo, conservando el de mayor severidad.

    Se deduplica por `codigo` y no por texto: el codigo es el catalogo cerrado,
    el texto puede variar entre validadores. Si dos validadores reportan el
    mismo codigo, gana el de severidad mas alta y se suma el monto impactado,
    porque pueden estar viendo porciones distintas del mismo problema.
    """
    por_codigo: dict[str, dict] = {}
    for h in hallazgos:
        codigo = h.get("codigo", "")
        previo = por_codigo.get(codigo)
        if previo is None:
            por_codigo[codigo] = dict(h)
            continue
        if config.PESO_SEVERIDAD.get(_sev(h).value, 0) > config.PESO_SEVERIDAD.get(
            _sev(previo).value, 0
        ):
            monto = max(
                float(previo.get("monto_impactado", 0.0)),
                float(h.get("monto_impactado", 0.0)),
            )
            por_codigo[codigo] = dict(h)
            por_codigo[codigo]["monto_impactado"] = monto
        else:
            por_codigo[codigo]["monto_impactado"] = max(
                float(previo.get("monto_impactado", 0.0)),
                float(h.get("monto_impactado", 0.0)),
            )
    # Orden estable: por severidad descendente, luego por codigo. Importa para
    # que el memo y los tests dorados sean reproducibles.
    return sorted(
        por_codigo.values(),
        key=lambda h: (-config.PESO_SEVERIDAD.get(_sev(h).value, 0), h.get("codigo", "")),
    )


def puntaje_determinista(
    hallazgos: list[dict], validadores_no_evaluables: list[str] | None = None
) -> int:
    """Puntaje 0-100 por suma de pesos de severidad, truncado.

    Se trunca y no se normaliza a proposito: una factura con cinco hallazgos
    criticos no es "mas riesgosa" que una con tres; las dos estan en el techo y
    las dos se retienen. Normalizar solo agregaria precision falsa.

    Cada validador que no pudo concluir suma `PESO_NO_EVALUABLE`: no saber es
    riesgo, no neutralidad.
    """
    total = sum(config.PESO_SEVERIDAD.get(_sev(h).value, 0) for h in hallazgos)
    total += config.PESO_NO_EVALUABLE * len(validadores_no_evaluables or [])
    return max(0, min(100, total))


def consolidar(
    id_factura: str,
    hallazgos: list[dict],
    validadores_no_evaluables: list[str] | None = None,
) -> dict:
    """Produce el ConsolidadoValidacion a partir de los hallazgos de los 4 validadores.

    Returns:
      Un dict con la forma de `ConsolidadoValidacion`, mas la clave `ruta` que
      `nodo_consolidar` usa para enrutar el grafo ('sin_hallazgos' dispara el
      atajo touchless, que ahorra 3 llamadas al modelo).
    """
    no_evaluables = list(validadores_no_evaluables or [])
    unicos = deduplicar(hallazgos)
    puntaje = puntaje_determinista(unicos, no_evaluables)
    sev_max = severidad_maxima([_sev(h) for h in unicos])

    monto_total = flt(
        redondear(sum((dec(h.get("monto_impactado", 0.0)) for h in unicos), dec(0)))
    )

    # Resumen por categoria, para el memo y para las metricas de observabilidad.
    por_categoria: dict[str, int] = {}
    for h in unicos:
        cat = str(h.get("categoria", "integridad_datos"))
        por_categoria[cat] = por_categoria.get(cat, 0) + 1

    return {
        "id_factura": id_factura,
        "hallazgos": unicos,
        "puntaje_determinista": puntaje,
        "severidad_maxima": sev_max.value,
        "monto_total_impactado": monto_total,
        "validadores_no_evaluables": no_evaluables,
        "resumen_por_categoria": [
            DatoVerificado(nombre=cat, valor=str(n)).model_dump()
            for cat, n in sorted(por_categoria.items())
        ],
        # No forma parte de ConsolidadoValidacion: la consume el grafo.
        "ruta": "sin_hallazgos" if not unicos and not no_evaluables else "con_hallazgos",
        "codigos": [h.get("codigo", "") for h in unicos],
        "codigos_bloqueantes": [
            h.get("codigo", "") for h in unicos if h.get("bloquea_pago")
        ],
    }
