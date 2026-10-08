"""Nodo de entrada del pipeline. Determinista, sin LLM.

Normaliza la factura, resuelve los lookups y deja en `ctx.state` todo lo que los
cuatro validadores necesitan. Tambien resetea el contador del ciclo del critico:
tiene que ser aqui y no en el replanteo, porque cada invocacion nueva empieza de
cero y un contador heredado de la corrida anterior agotaria el ciclo en la
primera vuelta.

Convencion que usan TODOS los FunctionNode de este paquete: reciben solo
`ctx: Context` y leen de `ctx.state` explicitamente, en lugar de declarar
parametros que ADK bindea desde el estado. Es mas verboso pero elimina dos
problemas: que un nodo falle porque una clave todavia no existe (el atajo
touchless se saltea agentes que escriben claves), y la validacion de
`state_schema` contra nombres de parametros.
"""

from __future__ import annotations

from google.adk.agents import Context
from google.adk.workflow import node

from ..herramientas import buscar_nr, buscar_oc, buscar_proveedor, cargar_factura
from ..herramientas.repositorio import DatoNoDisponible


def _extraer_id(node_input: object, ctx: Context) -> str:
    """Resuelve el id de la factura desde cualquiera de las tres vias de entrada.

    El pipeline arranca de tres formas distintas, y cada una entrega el id de
    una manera:

      1. `ctx.run_node(pipeline_ap, {"id_factura": ...})` — la herramienta
         `triar_factura` del coordinador. node_input es un dict.
      2. `Runner.run_async(state_delta={"id_factura": ...})` — un script o el
         Cloud Run Job de batch. Viene por estado.
      3. `adk run ap_ops_pipeline` / `adk web` — una persona escribe texto.
         El Runner entrega ese texto como node_input, asi que node_input es un
         str que puede ser el id pelado ('factura_conforme') o una frase
         ('triar factura_conforme').

    El type hint tiene que ser laxo: `node_input: dict` hace que ADK intente
    coercionar el str con Pydantic y falle antes de entrar a la funcion.
    """
    if isinstance(node_input, dict):
        candidato = str(node_input.get("id_factura") or "").strip()
        if candidato:
            return candidato

    del_estado = str(ctx.state.get("id_factura") or "").strip()
    if del_estado:
        return del_estado

    if isinstance(node_input, str):
        texto = node_input.strip()
        # De una frase se toma el ultimo token: ni los ids de negocio
        # ('20512345671-F001-00234') ni los nombres de fixture llevan espacios.
        return texto.split()[-1] if texto else ""

    return ""


@node(name="nodo_intake")
async def nodo_intake(ctx: Context, node_input: object = None) -> dict:
    """Carga la factura, resuelve los lookups y siembra el estado de la corrida.

    Es el unico nodo que mira `node_input`; el resto del grafo trabaja contra
    `ctx.state`.
    """
    id_entrada = _extraer_id(node_input, ctx)

    try:
        factura = cargar_factura(id_entrada)
    except DatoNoDisponible as e:
        # No hay factura: no hay nada que validar. Se siembra el estado minimo
        # para que los nodos de cierre puedan emitir un expediente coherente en
        # lugar de que el grafo explote a mitad de camino.
        ctx.state["error_intake"] = str(e)
        ctx.state["id_factura"] = id_entrada
        ctx.state["iter_critico"] = 0
        ctx.route = "error"
        return {"ok": False, "error": str(e), "id_factura": id_entrada}

    oc = buscar_oc(factura.orden_compra)
    nr = buscar_nr(factura.orden_compra)
    proveedor = buscar_proveedor(factura.ruc_proveedor)

    alertas: list[str] = list(factura.observaciones_ocr)
    if factura.orden_compra and oc is None:
        alertas.append(f"La orden de compra {factura.orden_compra} no existe en el sistema")
    if proveedor is None:
        alertas.append(f"El RUC {factura.ruc_proveedor} no esta en el maestro de proveedores")

    # --- estado de la corrida ------------------------------------------------
    # `id_factura` es la clave que interpolan las instrucciones de los cuatro
    # validadores con la sintaxis {id_factura}. Darsela en la instruccion y no
    # en el payload es mas confiable: el modelo no tiene que parsear nada.
    ctx.state["id_factura"] = factura.id_factura
    ctx.state["nombre_proveedor"] = factura.razon_social
    ctx.state["moneda"] = factura.moneda
    ctx.state["total_factura"] = factura.total
    ctx.state["factura"] = factura.model_dump()
    ctx.state["iter_critico"] = 0
    ctx.state["error_intake"] = None

    contexto = {
        "id_factura": factura.id_factura,
        "orden_compra_hallada": oc is not None,
        "nota_recepcion_hallada": nr is not None,
        "proveedor_hallado": proveedor is not None,
        "alertas_normalizacion": alertas,
    }
    ctx.state["contexto_validacion"] = contexto

    return {
        "ok": True,
        "id_factura": factura.id_factura,
        "proveedor": factura.razon_social,
        "moneda": factura.moneda,
        "total": factura.total,
        "confianza_ocr": factura.confianza_ocr,
        **contexto,
    }
