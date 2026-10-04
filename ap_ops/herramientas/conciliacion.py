"""Conciliacion a tres vias: factura <-> orden de compra <-> nota de recepcion.

El 3-way match es el control mas antiguo de Cuentas por Pagar y sigue siendo el
que atrapa mas plata. Responde tres preguntas, y fallar cualquiera es material:

    1. Lo pedimos?      -> existe la orden de compra, esta abierta, es del proveedor
    2. Lo recibimos?    -> existe la nota de recepcion y esta completa
    3. Nos cobran lo acordado? -> cantidades y precios coinciden dentro de tolerancia

La conciliacion es por linea, usando `codigo_articulo` como clave. Si la factura
no trae codigo de articulo, la comparacion cae a la descripcion, que es mucho
mas debil: eso se reporta como dato, no se oculta.
"""

from __future__ import annotations

from decimal import Decimal

from .. import config
from ..esquemas import LineaFactura
from . import catalogo, repositorio
from ._dinero import dec, flt, pct_diferencia, redondear


def _indexar(lineas: list[LineaFactura]) -> dict[str, LineaFactura]:
    """Indexa por codigo de articulo, con fallback a la descripcion normalizada."""
    indice: dict[str, LineaFactura] = {}
    for ln in lineas:
        clave = ln.codigo_articulo or f"desc::{ln.descripcion.strip().lower()}"
        indice[clave] = ln
    return indice


def _clave(ln: LineaFactura) -> str:
    return ln.codigo_articulo or f"desc::{ln.descripcion.strip().lower()}"


def analizar_tres_vias(id_factura: str) -> dict:
    """Concilia la factura contra su orden de compra y su nota de recepcion.

    Es la unica fuente de verdad para las diferencias de cantidad, precio y
    monto: ningun agente debe calcular estas diferencias por su cuenta.

    Args:
      id_factura: Identificador de la factura a conciliar.

    Returns:
      Los valores conciliados y las desviaciones detectadas. Si la factura no
      referencia una orden de compra, o la orden no existe, la conciliacion no
      es posible y eso se reporta como desviacion, no como error.
    """
    try:
        f = repositorio.cargar_factura(id_factura)
    except repositorio.DatoNoDisponible as e:
        return {"error": str(e), "datos_disponibles": False}

    conf = f.confianza_ocr
    if conf is not None and conf < config.CONFIANZA_OCR_MINIMA:
        return {
            "datos_disponibles": True,
            "confianza_insuficiente": True,
            "confianza_ocr": conf,
            "confianza_minima": config.CONFIANZA_OCR_MINIMA,
            "observaciones_ocr": f.observaciones_ocr,
            "desviaciones": [],
        }

    desviaciones: list[dict] = []
    total_factura = dec(f.total)

    # --- (1) existe la orden de compra? -------------------------------------
    if not f.orden_compra:
        desviaciones.append(
            catalogo.desviacion(
                "TRV-OC-001",
                "orden_compra",
                f"La factura {f.id_factura} por {f.moneda} {flt(total_factura):,.2f} "
                f"no referencia ninguna orden de compra: no hay conciliacion posible",
                monto_impactado=flt(total_factura),
            )
        )
        return _sin_conciliacion(f, desviaciones, oc_encontrada=False)

    try:
        oc = repositorio.buscar_oc(f.orden_compra)
        nr = repositorio.buscar_nr(f.orden_compra)
    except repositorio.DatoNoDisponible as e:
        # Falla de infraestructura, no hallazgo de negocio: el validador debe
        # responder 'no_evaluable' y el plugin de auditoria emitir una SenalOps.
        return {
            "datos_disponibles": False,
            "error": str(e),
            "desviaciones": [],
        }

    if oc is None:
        desviaciones.append(
            catalogo.desviacion(
                "TRV-OC-002",
                "orden_compra",
                f"La orden de compra '{f.orden_compra}' referenciada en la "
                f"factura no existe en el sistema",
                declarado=f.orden_compra,
                monto_impactado=flt(total_factura),
            )
        )
        return _sin_conciliacion(f, desviaciones, oc_encontrada=False)

    # --- (1b) la orden es utilizable y es del proveedor? --------------------
    if oc.estado != "abierta":
        desviaciones.append(
            catalogo.desviacion(
                "TRV-OC-003",
                "orden_compra",
                f"La orden de compra {oc.numero_oc} esta en estado "
                f"'{oc.estado}': no admite facturacion",
                declarado=oc.estado,
                esperado="abierta",
                monto_impactado=flt(total_factura),
            )
        )

    if oc.ruc_proveedor != f.ruc_proveedor:
        desviaciones.append(
            catalogo.desviacion(
                "TRV-OC-004",
                "ruc_proveedor",
                f"La factura es del RUC {f.ruc_proveedor} pero la orden de "
                f"compra {oc.numero_oc} fue emitida al RUC {oc.ruc_proveedor}",
                declarado=f.ruc_proveedor,
                esperado=oc.ruc_proveedor,
                monto_impactado=flt(total_factura),
            )
        )

    # --- (2) lo recibimos? ---------------------------------------------------
    if nr is None:
        desviaciones.append(
            catalogo.desviacion(
                "TRV-NR-005",
                "guia_remision",
                f"No existe nota de recepcion para la orden {oc.numero_oc}: "
                f"no hay constancia de que la mercaderia o el servicio se "
                f"haya recibido",
                monto_impactado=flt(total_factura),
            )
        )
    elif nr.estado != "completa":
        desviaciones.append(
            catalogo.desviacion(
                "TRV-NR-006",
                "guia_remision",
                f"La nota de recepcion {nr.numero_nr} esta en estado "
                f"'{nr.estado}': se factura mas de lo que consta recibido",
                declarado=nr.estado,
                esperado="completa",
            )
        )

    # --- (3) nos cobran lo acordado? ----------------------------------------
    idx_oc = _indexar(oc.lineas)
    idx_nr = _indexar(nr.lineas) if nr else {}

    conciliadas = 0
    con_diferencia = 0
    dif_cantidad_total = Decimal("0")
    sin_codigo = 0

    for ln in f.lineas:
        clave = _clave(ln)
        if clave.startswith("desc::"):
            sin_codigo += 1

        ln_oc = idx_oc.get(clave)
        if ln_oc is None:
            con_diferencia += 1
            desviaciones.append(
                catalogo.desviacion(
                    "TRV-LIN-010",
                    f"lineas[{ln.numero_linea - 1}]",
                    f"La linea {ln.numero_linea} ('{ln.descripcion}', "
                    f"codigo {ln.codigo_articulo or 'sin codigo'}) por "
                    f"{flt(dec(ln.importe)):,.2f} no figura en la orden "
                    f"de compra {oc.numero_oc}",
                    declarado=ln.codigo_articulo or ln.descripcion,
                    monto_impactado=flt(dec(ln.importe)),
                )
            )
            continue

        conciliadas += 1
        hay_dif = False

        # cantidad: contra la nota de recepcion si existe, si no contra la OC
        ln_ref = idx_nr.get(clave) or ln_oc
        cant_f = dec(ln.cantidad)
        cant_ref = dec(ln_ref.cantidad)
        dif_cant = redondear(cant_f - cant_ref)
        dif_cantidad_total += dif_cant
        pct_cant = pct_diferencia(cant_f, cant_ref)

        if abs(pct_cant) > dec(config.TOLERANCIA_CANTIDAD_PCT):
            hay_dif = True
            fuente_ref = f"nota de recepcion {nr.numero_nr}" if idx_nr.get(clave) else f"OC {oc.numero_oc}"
            desviaciones.append(
                catalogo.desviacion(
                    "TRV-CAN-007",
                    f"lineas[{ln.numero_linea - 1}].cantidad",
                    f"Linea {ln.numero_linea}: facturadas {flt(cant_f):,.2f} "
                    f"{ln.unidad_medida} vs {flt(cant_ref):,.2f} segun "
                    f"{fuente_ref}; diferencia {flt(dif_cant):+,.2f} "
                    f"({flt(pct_cant):+,.2f}%, tolerancia "
                    f"{config.TOLERANCIA_CANTIDAD_PCT:.1f}%)",
                    declarado=f"{flt(cant_f):,.2f}",
                    esperado=f"{flt(cant_ref):,.2f}",
                    monto_impactado=abs(flt(redondear(dif_cant * dec(ln.precio_unitario)))),
                )
            )

        # precio unitario: siempre contra la OC (es el precio pactado)
        precio_f = dec(ln.precio_unitario)
        precio_oc = dec(ln_oc.precio_unitario)
        pct_precio = pct_diferencia(precio_f, precio_oc)

        if abs(pct_precio) > dec(config.TOLERANCIA_PRECIO_PCT):
            hay_dif = True
            desviaciones.append(
                catalogo.desviacion(
                    "TRV-PRE-008",
                    f"lineas[{ln.numero_linea - 1}].precio_unitario",
                    f"Linea {ln.numero_linea}: precio facturado "
                    f"{flt(precio_f):,.2f} vs {flt(precio_oc):,.2f} pactado en "
                    f"la OC {oc.numero_oc}; diferencia {flt(pct_precio):+,.2f}% "
                    f"(tolerancia {config.TOLERANCIA_PRECIO_PCT:.1f}%)",
                    declarado=f"{flt(precio_f):,.2f}",
                    esperado=f"{flt(precio_oc):,.2f}",
                    monto_impactado=abs(flt(redondear((precio_f - precio_oc) * cant_f))),
                )
            )

        if hay_dif:
            con_diferencia += 1

    # --- monto total vs disponible de la OC ---------------------------------
    subtotal_f = dec(f.subtotal)
    autorizado = dec(oc.monto_autorizado)
    consumido = dec(oc.monto_consumido)
    disponible = redondear(autorizado - consumido)
    dif_monto = redondear(subtotal_f - disponible)
    pct_monto = pct_diferencia(subtotal_f, disponible)

    if dif_monto > dec(0) and abs(pct_monto) > dec(config.TOLERANCIA_MONTO_PCT):
        desviaciones.append(
            catalogo.desviacion(
                "TRV-MON-009",
                "subtotal",
                f"Subtotal facturado {flt(subtotal_f):,.2f} excede el "
                f"disponible de la OC {oc.numero_oc}: autorizado "
                f"{flt(autorizado):,.2f} menos consumido {flt(consumido):,.2f} "
                f"= {flt(disponible):,.2f}; exceso {flt(dif_monto):+,.2f} "
                f"({flt(pct_monto):+,.2f}%)",
                declarado=f"{flt(subtotal_f):,.2f}",
                esperado=f"{flt(disponible):,.2f}",
                monto_impactado=flt(dif_monto),
            )
        )

    dentro_tolerancia = con_diferencia == 0 and dif_monto <= dec(0)

    return {
        "datos_disponibles": True,
        "confianza_insuficiente": False,
        "id_factura": f.id_factura,
        "orden_compra_encontrada": True,
        "numero_oc": oc.numero_oc,
        "estado_oc": oc.estado,
        "nota_recepcion_encontrada": nr is not None,
        "numero_nr": nr.numero_nr if nr else None,
        "estado_nr": nr.estado if nr else None,
        "lineas_facturadas": len(f.lineas),
        "lineas_conciliadas": conciliadas,
        "lineas_con_diferencia": con_diferencia,
        "lineas_sin_codigo_articulo": sin_codigo,
        "diferencia_cantidad_total": flt(dif_cantidad_total),
        "monto_autorizado": flt(autorizado),
        "monto_consumido_oc": flt(consumido),
        "monto_disponible_oc": flt(disponible),
        "diferencia_monto": flt(dif_monto),
        "diferencia_porcentaje": flt(pct_monto),
        "dentro_tolerancia": dentro_tolerancia,
        "tolerancia_cantidad_pct": config.TOLERANCIA_CANTIDAD_PCT,
        "tolerancia_precio_pct": config.TOLERANCIA_PRECIO_PCT,
        "tolerancia_monto_pct": config.TOLERANCIA_MONTO_PCT,
        "desviaciones": desviaciones,
    }


def _sin_conciliacion(f, desviaciones: list[dict], oc_encontrada: bool) -> dict:
    """Respuesta cuando no hay OC contra la que conciliar.

    Devuelve la misma forma que el camino normal, con los campos de
    conciliacion en cero. Mantener la forma estable importa: el esquema
    `ResultadoTresVias` los exige, y un dict con claves faltantes haria que el
    modelo las invente.
    """
    return {
        "datos_disponibles": True,
        "confianza_insuficiente": False,
        "id_factura": f.id_factura,
        "orden_compra_encontrada": oc_encontrada,
        "numero_oc": f.orden_compra,
        "estado_oc": None,
        "nota_recepcion_encontrada": False,
        "numero_nr": None,
        "estado_nr": None,
        "lineas_facturadas": len(f.lineas),
        "lineas_conciliadas": 0,
        "lineas_con_diferencia": 0,
        "lineas_sin_codigo_articulo": sum(1 for ln in f.lineas if not ln.codigo_articulo),
        "diferencia_cantidad_total": 0.0,
        "monto_autorizado": 0.0,
        "monto_consumido_oc": 0.0,
        "monto_disponible_oc": 0.0,
        "diferencia_monto": 0.0,
        "diferencia_porcentaje": 0.0,
        "dentro_tolerancia": False,
        "tolerancia_cantidad_pct": config.TOLERANCIA_CANTIDAD_PCT,
        "tolerancia_precio_pct": config.TOLERANCIA_PRECIO_PCT,
        "tolerancia_monto_pct": config.TOLERANCIA_MONTO_PCT,
        "desviaciones": desviaciones,
    }
