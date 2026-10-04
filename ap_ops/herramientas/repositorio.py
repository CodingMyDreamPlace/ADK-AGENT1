"""Acceso a datos. ESTE ES EL UNICO SEAM A BIGQUERY / ERP.

Es el unico modulo del paquete que sabe de donde vienen los datos. Todo lo que
esta por encima (validadores, agentes, grafo, esquemas, tests) trabaja contra
modelos Pydantic ya construidos y no tiene idea de si salieron de un JSON local,
de BigQuery o de la API de un ERP.

Migracion a produccion (etapa C del camino en ARQUITECTURA.md): se reemplaza el
cuerpo de estas funciones por `BigQueryToolset.execute_sql` de
`google.adk.integrations.bigquery` con `BigQueryToolConfig(write_mode=WriteMode.BLOCKED)`,
o por llamadas REST al ERP. Las firmas no cambian, asi que nada mas se toca.

Los fixtures se cargan una sola vez y quedan en cache de modulo: son datos de
solo lectura y el pipeline los consulta varias veces por factura.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..esquemas import (
    Factura,
    NotaRecepcion,
    OrdenCompra,
    ProveedorMaestro,
)

DIR_DATOS = Path(__file__).resolve().parent.parent / "datos"
DIR_FACTURAS = DIR_DATOS / "facturas"


class DatoNoDisponible(RuntimeError):
    """El insumo no se pudo leer (archivo ausente, JSON corrupto, tabla caida).

    Se distingue deliberadamente de "no existe": un proveedor que no esta en el
    maestro es un hallazgo de negocio; un maestro que no se puede leer es una
    falla de infraestructura. La primera produce un Hallazgo; la segunda
    produce estado 'no_evaluable' mas una SenalOps.
    """


def _leer_json(ruta: Path) -> object:
    if not ruta.exists():
        raise DatoNoDisponible(f"No existe el archivo de datos: {ruta.name}")
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DatoNoDisponible(f"JSON invalido en {ruta.name}: {e}") from e


# --------------------------------------------------------------- facturas


def listar_facturas() -> list[str]:
    """Nombres de los fixtures disponibles, sin extension."""
    if not DIR_FACTURAS.exists():
        return []
    return sorted(p.stem for p in DIR_FACTURAS.glob("*.json"))


def cargar_factura(nombre_o_id: str) -> Factura:
    """Carga una factura por nombre de fixture o por `id_factura`.

    Acepta las dos formas porque el script de smoke trabaja con nombres de
    archivo ('factura_igv_erroneo') y el coordinador con ids de negocio
    ('20512345678-F001-00234').
    """
    directa = DIR_FACTURAS / f"{nombre_o_id}.json"
    if directa.exists():
        return Factura.model_validate(_leer_json(directa))

    for ruta in sorted(DIR_FACTURAS.glob("*.json")):
        datos = _leer_json(ruta)
        if isinstance(datos, dict) and datos.get("id_factura") == nombre_o_id:
            return Factura.model_validate(datos)

    raise DatoNoDisponible(f"No se encontro la factura '{nombre_o_id}'")


# --------------------------------------------------------------- maestros


@lru_cache(maxsize=1)
def _ordenes() -> dict[str, OrdenCompra]:
    datos = _leer_json(DIR_DATOS / "ordenes_compra.json")
    assert isinstance(datos, list)
    return {d["numero_oc"]: OrdenCompra.model_validate(d) for d in datos}


@lru_cache(maxsize=1)
def _recepciones() -> list[NotaRecepcion]:
    datos = _leer_json(DIR_DATOS / "notas_recepcion.json")
    assert isinstance(datos, list)
    return [NotaRecepcion.model_validate(d) for d in datos]


@lru_cache(maxsize=1)
def _proveedores() -> dict[str, ProveedorMaestro]:
    datos = _leer_json(DIR_DATOS / "maestro_proveedores.json")
    assert isinstance(datos, list)
    return {d["ruc"]: ProveedorMaestro.model_validate(d) for d in datos}


@lru_cache(maxsize=1)
def _historico() -> list[dict]:
    datos = _leer_json(DIR_DATOS / "facturas_historicas.json")
    assert isinstance(datos, list)
    return datos


def buscar_oc(numero_oc: str | None) -> OrdenCompra | None:
    """Devuelve None si la OC no existe. Eso es un hallazgo, no un error."""
    if not numero_oc:
        return None
    return _ordenes().get(numero_oc)


def buscar_nr(numero_oc: str | None) -> NotaRecepcion | None:
    """Nota de recepcion asociada a una OC.

    Si hay varias (entregas parciales), devuelve la mas reciente: es la que
    refleja el acumulado recibido.
    """
    if not numero_oc:
        return None
    candidatas = [n for n in _recepciones() if n.numero_oc == numero_oc]
    if not candidatas:
        return None
    return max(candidatas, key=lambda n: n.fecha_recepcion)


def buscar_proveedor(ruc: str) -> ProveedorMaestro | None:
    """Devuelve None si el RUC no esta en el maestro. Hallazgo, no error."""
    return _proveedores().get(ruc)


def buscar_historial(ruc: str, excluir_id: str | None = None) -> list[dict]:
    """Facturas historicas del proveedor, para deteccion de duplicados.

    Devuelve dicts crudos y no modelos Pydantic a proposito: el historico es
    una proyeccion de pocas columnas (id, huella, total, fecha, estado de pago),
    no la factura completa. En BigQuery esto es un SELECT de 5 columnas con
    filtro por RUC, no un `SELECT *`.
    """
    return [
        f
        for f in _historico()
        if f.get("ruc_proveedor") == ruc and f.get("id_factura") != excluir_id
    ]


def limpiar_cache() -> None:
    """Invalida los caches. Para los tests que manipulan fixtures en disco."""
    _ordenes.cache_clear()
    _recepciones.cache_clear()
    _proveedores.cache_clear()
    _historico.cache_clear()
