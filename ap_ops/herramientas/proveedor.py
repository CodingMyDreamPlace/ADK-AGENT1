"""Habilitacion del proveedor y limites contractuales.

Los hallazgos de este validador son los menos vistosos y los que mas discusion
generan, porque la factura puede estar aritmeticamente perfecta y conciliar al
centavo con la OC, y aun asi no deberia pagarse:

  * un proveedor suspendido no deberia recibir pagos nuevos,
  * una factura a contado contra un contrato a 60 dias adelanta capital de
    trabajo sin autorizacion —es una decision financiera real, no un detalle
    administrativo—,
  * facturar por encima del limite de contrato compromete a la empresa mas alla
    de lo que alguien aprobo.
"""

from __future__ import annotations

from .. import config
from . import catalogo, repositorio
from ._dinero import dec, flt, redondear
from ._fechas import dias_desde


def analizar_proveedor_contrato(id_factura: str) -> dict:
    """Verifica habilitacion del proveedor, condicion de pago y limite de contrato.

    Es la unica fuente de verdad para el saldo disponible de contrato: ningun
    agente debe calcular el consumo por su cuenta.

    Args:
      id_factura: Identificador de la factura a analizar.

    Returns:
      Los valores del maestro y las desviaciones detectadas. Si el proveedor no
      esta en el maestro, lo reporta como desviacion PRV-MAE-001 (hallazgo de
      negocio) y no como error de infraestructura.
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

    try:
        proveedor = repositorio.buscar_proveedor(f.ruc_proveedor)
    except repositorio.DatoNoDisponible as e:
        # El maestro no se pudo leer. Esto SI es infraestructura: el validador
        # debe responder 'no_evaluable' y el plugin emitir una SenalOps.
        return {"datos_disponibles": False, "error": str(e), "desviaciones": []}

    desviaciones: list[dict] = []
    total = dec(f.total)

    # --- proveedor no registrado --------------------------------------------
    if proveedor is None:
        desviaciones.append(
            catalogo.desviacion(
                "PRV-MAE-001",
                "ruc_proveedor",
                f"El RUC {f.ruc_proveedor} ('{f.razon_social}') no esta "
                f"registrado en el maestro de proveedores. No hay cuenta "
                f"bancaria registrada contra la que comparar, ni condiciones "
                f"contractuales que verificar",
                declarado=f.ruc_proveedor,
                monto_impactado=flt(total),
            )
        )
        return {
            "datos_disponibles": True,
            "confianza_insuficiente": False,
            "id_factura": f.id_factura,
            "proveedor_en_maestro": False,
            "proveedor_habilitado": False,
            "estado_proveedor": "no_registrado",
            "dias_desde_alta": None,
            "condicion_pago_declarada": f.condicion_pago,
            "condicion_pago_contractual": None,
            "condicion_pago_coincide": False,
            "razon_social_coincide": False,
            "limite_contrato": None,
            "monto_consumido": None,
            "monto_disponible": None,
            "excede_contrato": False,
            "exceso_contrato": 0.0,
            "categoria_riesgo": "alto",
            "es_agente_retencion": False,
            "desviaciones": desviaciones,
        }

    # --- estado de habilitacion ---------------------------------------------
    habilitado = proveedor.estado in ("habilitado", "nuevo")
    if not habilitado:
        desviaciones.append(
            catalogo.desviacion(
                "PRV-EST-002",
                "ruc_proveedor",
                f"El proveedor {proveedor.razon_social} (RUC {proveedor.ruc}) "
                f"esta en estado '{proveedor.estado}' en el maestro: no admite "
                f"pagos nuevos por {f.moneda} {flt(total):,.2f}",
                declarado=proveedor.estado,
                esperado="habilitado",
                monto_impactado=flt(total),
            )
        )

    # --- razon social --------------------------------------------------------
    def _norm(s: str) -> str:
        return "".join(c for c in s.upper() if c.isalnum())

    razon_coincide = _norm(f.razon_social) == _norm(proveedor.razon_social)
    if not razon_coincide:
        desviaciones.append(
            catalogo.desviacion(
                "PRV-RAZ-005",
                "razon_social",
                f"La razon social de la factura ('{f.razon_social}') difiere de "
                f"la registrada en el maestro ('{proveedor.razon_social}') para "
                f"el RUC {proveedor.ruc}",
                declarado=f.razon_social,
                esperado=proveedor.razon_social,
            )
        )

    # --- condicion de pago ---------------------------------------------------
    cond_coincide = f.condicion_pago == proveedor.condicion_pago_contractual
    if not cond_coincide:
        desviaciones.append(
            catalogo.desviacion(
                "PRV-CND-003",
                "condicion_pago",
                f"La factura exige '{f.condicion_pago}' pero el contrato "
                f"establece '{proveedor.condicion_pago_contractual}'. "
                f"Pagar antes de lo pactado adelanta "
                f"{f.moneda} {flt(total):,.2f} de capital de trabajo sin "
                f"autorizacion",
                declarado=f.condicion_pago,
                esperado=proveedor.condicion_pago_contractual,
                monto_impactado=flt(total),
            )
        )

    # --- limite de contrato --------------------------------------------------
    limite = proveedor.limite_contrato
    consumido = dec(proveedor.monto_consumido_contrato)
    excede = False
    exceso = dec(0)
    disponible = None

    if limite is not None:
        lim = dec(limite)
        disponible = redondear(lim - consumido)
        proyectado = redondear(consumido + total)
        if proyectado > lim:
            excede = True
            exceso = redondear(proyectado - lim)
            desviaciones.append(
                catalogo.desviacion(
                    "PRV-LIM-004",
                    "total",
                    f"Limite de contrato {flt(lim):,.2f}, consumido "
                    f"{flt(consumido):,.2f}, disponible {flt(disponible):,.2f}. "
                    f"Esta factura de {flt(total):,.2f} lleva el consumido a "
                    f"{flt(proyectado):,.2f} y excede el limite en "
                    f"{flt(exceso):,.2f}",
                    declarado=f"{flt(total):,.2f}",
                    esperado=f"<= {flt(disponible):,.2f}",
                    monto_impactado=flt(exceso),
                )
            )

    return {
        "datos_disponibles": True,
        "confianza_insuficiente": False,
        "id_factura": f.id_factura,
        "proveedor_en_maestro": True,
        "proveedor_habilitado": habilitado,
        "estado_proveedor": proveedor.estado,
        "dias_desde_alta": dias_desde(proveedor.fecha_alta),
        "razon_social_maestro": proveedor.razon_social,
        "razon_social_coincide": razon_coincide,
        "condicion_pago_declarada": f.condicion_pago,
        "condicion_pago_contractual": proveedor.condicion_pago_contractual,
        "condicion_pago_coincide": cond_coincide,
        "limite_contrato": limite,
        "monto_consumido": flt(consumido),
        "monto_disponible": flt(disponible) if disponible is not None else None,
        "excede_contrato": excede,
        "exceso_contrato": flt(exceso),
        "categoria_riesgo": proveedor.categoria_riesgo,
        "es_agente_retencion": proveedor.es_agente_retencion,
        "desviaciones": desviaciones,
    }
