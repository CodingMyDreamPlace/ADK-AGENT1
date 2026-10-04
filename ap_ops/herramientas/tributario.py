"""Analisis tributario. 100% determinista, sin LLM.

Esta es la herramienta que el `validador_tributario` esta OBLIGADO a llamar. Su
prompt dice textualmente que es la unica fuente de verdad numerica y que no debe
recalcular nada por su cuenta.

El motivo es concreto: la aritmetica es el modo de falla numero uno de un LLM en
Cuentas por Pagar, y es exactamente lo que un auditor va a re-verificar con una
calculadora. Un IGV mal calculado por el modelo no es un error cosmetico: es un
reparo tributario.
"""

from __future__ import annotations

from decimal import Decimal

from .. import config
from ..esquemas import NOMBRE_COMPROBANTE
from . import catalogo, repositorio
from ._dinero import dec, difiere, flt, redondear
from ._fechas import dias_desde, parsear
from ._ruc import validar_ruc


def analizar_tributario(id_factura: str) -> dict:
    """Valida IGV, aritmetica, RUC, tipo de comprobante, periodo y detraccion.

    Es la unica fuente de verdad numerica para el validador tributario: ningun
    agente debe recalcular IGV, sumas ni diferencias por su cuenta.

    Args:
      id_factura: Identificador de la factura a analizar.

    Returns:
      Los valores calculados y una lista de desviaciones detectadas. Si la
      confianza del OCR es insuficiente devuelve `confianza_insuficiente=True`
      y la lista de desviaciones vacia: validar cifras mal leidas produce
      hallazgos falsos.
    """
    try:
        f = repositorio.cargar_factura(id_factura)
    except repositorio.DatoNoDisponible as e:
        return {"error": str(e), "datos_disponibles": False}

    # --- compuerta de confianza: antes que cualquier calculo -----------------
    conf = f.confianza_ocr
    if conf is not None and conf < config.CONFIANZA_OCR_MINIMA:
        return {
            "datos_disponibles": True,
            "confianza_insuficiente": True,
            "confianza_ocr": conf,
            "confianza_minima": config.CONFIANZA_OCR_MINIMA,
            "observaciones_ocr": f.observaciones_ocr,
            "desviaciones": [
                catalogo.desviacion(
                    "INT-OCR-001",
                    "confianza_ocr",
                    f"Confianza de extraccion {conf:.2f} por debajo del minimo "
                    f"{config.CONFIANZA_OCR_MINIMA:.2f}. "
                    f"Observaciones del OCR: {'; '.join(f.observaciones_ocr) or 'ninguna'}",
                    declarado=f"{conf:.2f}",
                    esperado=f">= {config.CONFIANZA_OCR_MINIMA:.2f}",
                )
            ],
        }

    desviaciones: list[dict] = []

    # --- RUC ----------------------------------------------------------------
    ruc_valido, ruc_tipo = validar_ruc(f.ruc_proveedor)
    if not ruc_valido:
        desviaciones.append(
            catalogo.desviacion(
                "TRI-RUC-003",
                "ruc_proveedor",
                f"El RUC '{f.ruc_proveedor}' no pasa la validacion de digito "
                f"verificador modulo 11 (tipo detectado: {ruc_tipo})",
                declarado=f.ruc_proveedor,
            )
        )

    # --- IGV ----------------------------------------------------------------
    subtotal = dec(f.subtotal)
    igv_declarado = dec(f.igv)
    igv_calculado = redondear(subtotal * dec(config.TASA_IGV))
    diferencia_igv = redondear(igv_declarado - igv_calculado)

    igv_correcto = not difiere(igv_declarado, igv_calculado, config.TOLERANCIA_IGV)
    if not igv_correcto:
        desviaciones.append(
            catalogo.desviacion(
                "TRI-IGV-001",
                "igv",
                f"subtotal {flt(subtotal):,.2f} x {config.TASA_IGV:.0%} = "
                f"{flt(igv_calculado):,.2f}; declarado {flt(igv_declarado):,.2f}; "
                f"diferencia {flt(diferencia_igv):+,.2f} "
                f"(tolerancia S/ {config.TOLERANCIA_IGV:.2f})",
                declarado=f"{flt(igv_declarado):,.2f}",
                esperado=f"{flt(igv_calculado):,.2f}",
                monto_impactado=abs(flt(diferencia_igv)),
            )
        )

    # --- aritmetica del total ------------------------------------------------
    otros = dec(f.otros_cargos)
    total_declarado = dec(f.total)
    total_calculado = redondear(subtotal + igv_declarado + otros)
    diferencia_total = redondear(total_declarado - total_calculado)

    aritmetica_correcta = not difiere(
        total_declarado, total_calculado, config.TOLERANCIA_ARITMETICA
    )
    if not aritmetica_correcta:
        desviaciones.append(
            catalogo.desviacion(
                "TRI-ARI-002",
                "total",
                f"subtotal {flt(subtotal):,.2f} + igv {flt(igv_declarado):,.2f} + "
                f"otros {flt(otros):,.2f} = {flt(total_calculado):,.2f}; "
                f"total declarado {flt(total_declarado):,.2f}; "
                f"diferencia {flt(diferencia_total):+,.2f}",
                declarado=f"{flt(total_declarado):,.2f}",
                esperado=f"{flt(total_calculado):,.2f}",
                monto_impactado=abs(flt(diferencia_total)),
            )
        )

    # --- suma de lineas vs subtotal ------------------------------------------
    suma_lineas = redondear(sum((dec(ln.importe) for ln in f.lineas), Decimal("0")))
    lineas_cuadran = not difiere(suma_lineas, subtotal, config.TOLERANCIA_ARITMETICA)
    if not lineas_cuadran:
        desviaciones.append(
            catalogo.desviacion(
                "TRI-LIN-007",
                "lineas",
                f"suma de {len(f.lineas)} linea(s) = {flt(suma_lineas):,.2f}; "
                f"subtotal declarado {flt(subtotal):,.2f}; "
                f"diferencia {flt(redondear(suma_lineas - subtotal)):+,.2f}",
                declarado=f"{flt(subtotal):,.2f}",
                esperado=f"{flt(suma_lineas):,.2f}",
                monto_impactado=abs(flt(redondear(suma_lineas - subtotal))),
            )
        )

    # --- tipo de comprobante -------------------------------------------------
    # Solo la factura (01) da derecho a credito fiscal del IGV. Una boleta (03)
    # en el flujo de AP es un hallazgo en si mismo.
    tipo_valido = f.tipo_comprobante == "01"
    if not tipo_valido:
        nombre = NOMBRE_COMPROBANTE.get(f.tipo_comprobante, "desconocido")
        desviaciones.append(
            catalogo.desviacion(
                "TRI-CMP-004",
                "tipo_comprobante",
                f"Tipo '{f.tipo_comprobante}' ({nombre}) no otorga derecho a "
                f"credito fiscal de IGV. Se requiere '01' (Factura)",
                declarado=f"{f.tipo_comprobante} ({nombre})",
                esperado="01 (Factura)",
                monto_impactado=flt(igv_declarado),
            )
        )

    # --- periodo -------------------------------------------------------------
    dias = dias_desde(f.fecha_emision)
    periodo_valido = True
    if dias is None:
        periodo_valido = False
        desviaciones.append(
            catalogo.desviacion(
                "TRI-PER-005",
                "fecha_emision",
                f"Fecha de emision ilegible o ausente: '{f.fecha_emision}'",
                declarado=str(f.fecha_emision),
            )
        )
    elif dias < 0:
        periodo_valido = False
        desviaciones.append(
            catalogo.desviacion(
                "TRI-FUT-008",
                "fecha_emision",
                f"Fecha de emision {f.fecha_emision} es posterior a la fecha "
                f"de analisis ({abs(dias)} dias en el futuro)",
                declarado=f.fecha_emision,
            )
        )
    elif dias > config.DIAS_ANTIGUEDAD_MAX:
        periodo_valido = False
        desviaciones.append(
            catalogo.desviacion(
                "TRI-PER-005",
                "fecha_emision",
                f"Comprobante emitido hace {dias} dias, supera el maximo "
                f"deducible de {config.DIAS_ANTIGUEDAD_MAX} dias",
                declarado=f.fecha_emision,
                esperado=f"<= {config.DIAS_ANTIGUEDAD_MAX} dias de antiguedad",
                monto_impactado=flt(igv_declarado),
            )
        )

    # --- detraccion (SPOT) ---------------------------------------------------
    codigo_det = f.codigo_detraccion
    detraccion_requerida = bool(
        (codigo_det or f.detraccion_aplica)
        and flt(total_declarado) > config.DETRACCION_MONTO_MINIMO
    )
    porcentaje_det = config.TASAS_DETRACCION.get(codigo_det or "")
    monto_det = (
        flt(redondear(total_declarado * dec(porcentaje_det)))
        if detraccion_requerida and porcentaje_det
        else 0.0
    )
    cuenta_det_presente = f.cuenta_bancaria.tipo_cuenta == "detracciones"

    if detraccion_requerida and not cuenta_det_presente:
        desviaciones.append(
            catalogo.desviacion(
                "TRI-DET-006",
                "cuenta_bancaria.tipo_cuenta",
                f"Detraccion aplicable (codigo {codigo_det or 'no declarado'}, "
                f"{(porcentaje_det or 0):.0%} de {flt(total_declarado):,.2f} = "
                f"S/ {monto_det:,.2f}) pero la cuenta de abono es de tipo "
                f"'{f.cuenta_bancaria.tipo_cuenta}', no 'detracciones'",
                declarado=f.cuenta_bancaria.tipo_cuenta,
                esperado="detracciones",
                monto_impactado=monto_det,
            )
        )

    return {
        "datos_disponibles": True,
        "confianza_insuficiente": False,
        "id_factura": f.id_factura,
        # RUC
        "ruc_valido": ruc_valido,
        "ruc_tipo": ruc_tipo,
        # IGV
        "igv_declarado": flt(igv_declarado),
        "igv_calculado": flt(igv_calculado),
        "diferencia_igv": flt(diferencia_igv),
        "igv_correcto": igv_correcto,
        "tasa_igv_aplicada": config.TASA_IGV,
        "tolerancia_igv": config.TOLERANCIA_IGV,
        # aritmetica
        "subtotal_declarado": flt(subtotal),
        "total_declarado": flt(total_declarado),
        "total_calculado": flt(total_calculado),
        "diferencia_total": flt(diferencia_total),
        "aritmetica_correcta": aritmetica_correcta,
        "suma_lineas": flt(suma_lineas),
        "lineas_cuadran": lineas_cuadran,
        # comprobante y periodo
        "tipo_comprobante": f.tipo_comprobante,
        "nombre_comprobante": NOMBRE_COMPROBANTE.get(f.tipo_comprobante, "desconocido"),
        "tipo_comprobante_valido": tipo_valido,
        "dias_antiguedad": dias if dias is not None else -1,
        "periodo_valido": periodo_valido,
        # detraccion
        "detraccion_requerida": detraccion_requerida,
        "codigo_detraccion": codigo_det,
        "porcentaje_detraccion": porcentaje_det,
        "monto_detraccion": monto_det,
        "cuenta_detraccion_presente": cuenta_det_presente,
        # material para el modelo
        "desviaciones": desviaciones,
    }
