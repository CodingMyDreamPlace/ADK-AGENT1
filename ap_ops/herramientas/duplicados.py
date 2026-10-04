"""Duplicados y senales de fraude.

Es la herramienta de mayor rendimiento del pipeline, y vale la pena entender por
que: el fraude clasico de Cuentas por Pagar no consiste en inventar una factura
falsa. Consiste en interceptar una factura REAL y cambiarle el numero de cuenta
de destino. Todo lo demas cuadra —la OC existe, la mercaderia llego, el IGV esta
bien calculado— y el pago sale al delincuente.

La unica defensa es comparar el CCI declarado contra el registrado en el maestro
de proveedores. Esa comparacion tiene que ser EXACTA, nunca "se parece": es
precisamente la clase de chequeo que no se le puede delegar a un modelo.

Tres senales que por separado son debiles y juntas son un fraude en curso:
  * cuenta de abono distinta de la registrada
  * proveedor dado de alta hace pocos dias
  * monto apenas por debajo del umbral de aprobacion

Esta herramienta las detecta por separado. Correlacionarlas es trabajo del
`agente_scoring_riesgo`, porque la conclusion "esto es un fraude" no sale de
sumar los tres puntajes.
"""

from __future__ import annotations

import hashlib

from .. import config
from ..esquemas import Factura
from . import catalogo, repositorio
from ._dinero import dec, enmascarar_cuenta, flt, redondear
from ._fechas import dias_desde


def huella(ruc: str, serie: str, numero: str, total: float, fecha: str) -> str:
    """Huella de una factura, para deteccion de duplicados.

    Se construye con los cinco campos que un duplicado real comparte. No se
    incluye `id_factura` porque un reenvio puede traer el id normalizado de
    otra forma, ni las lineas porque un duplicado puede venir con el detalle
    reordenado.

    Monto redondeado a 2 decimales para que un centavo de diferencia de
    redondeo no haga que dos huellas identicas se vean distintas.
    """
    crudo = f"{ruc}|{serie}|{numero}|{flt(dec(total)):.2f}|{fecha[:10]}"
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]


def _cuentas_coinciden(f: Factura, cuentas_registradas: list) -> tuple[bool, str | None]:
    """Compara el CCI declarado contra TODAS las cuentas registradas.

    Un proveedor legitimo puede tener cuenta corriente y cuenta de detracciones,
    asi que basta con coincidir con alguna. Si la factura no trae CCI se cae a
    comparar el numero de cuenta, que es mas debil pero mejor que nada.
    """
    cci_declarado = (f.cuenta_bancaria.cci or "").strip()
    num_declarado = "".join(c for c in f.cuenta_bancaria.numero_cuenta if c.isdigit())

    for cuenta in cuentas_registradas:
        cci_reg = (cuenta.cci or "").strip()
        if cci_declarado and cci_reg and cci_declarado == cci_reg:
            return True, cci_reg
        num_reg = "".join(c for c in cuenta.numero_cuenta if c.isdigit())
        if not cci_declarado and num_declarado and num_declarado == num_reg:
            return True, cci_reg or num_reg

    primera = cuentas_registradas[0] if cuentas_registradas else None
    return False, (primera.cci or primera.numero_cuenta) if primera else None


def analizar_duplicados_fraude(id_factura: str) -> dict:
    """Detecta duplicados y senales de fraude sobre la factura.

    Es la unica fuente de verdad para la comparacion de cuenta bancaria: ningun
    agente debe juzgar si dos cuentas "se parecen". La comparacion es exacta.

    Args:
      id_factura: Identificador de la factura a analizar.

    Returns:
      Los indicadores calculados y las desviaciones detectadas. Las cuentas se
      devuelven SIEMPRE enmascaradas: este resultado viaja a prompts, estado de
      sesion, base de datos y trazas de OpenTelemetry.
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
    total = dec(f.total)
    mi_huella = huella(f.ruc_proveedor, f.serie, f.numero, f.total, f.fecha_emision)

    # --- duplicados ----------------------------------------------------------
    # NO se excluye la factura por id: la factura bajo triaje todavia no esta en
    # el registro de pagos, asi que un id que YA figura es exactamente el caso
    # que queremos atrapar. Excluirla a si misma enmascararia el duplicado.
    try:
        historial = repositorio.buscar_historial(f.ruc_proveedor)
    except repositorio.DatoNoDisponible as e:
        return {"datos_disponibles": False, "error": str(e), "desviaciones": []}

    coincidencias: list[dict] = []
    for prev in historial:
        h_prev = huella(
            prev.get("ruc_proveedor", ""),
            prev.get("serie", ""),
            prev.get("numero", ""),
            float(prev.get("total", 0.0)),
            prev.get("fecha_emision", ""),
        )
        tipo = None
        similitud = 0.0

        if prev.get("id_factura") == f.id_factura:
            tipo, similitud = "exacta", 1.0
        elif h_prev == mi_huella:
            tipo, similitud = "huella", 1.0
        elif (
            abs(dec(prev.get("total", 0.0)) - total) <= dec(0.01)
            and prev.get("fecha_emision", "")[:10] == f.fecha_emision[:10]
        ):
            tipo, similitud = "monto_fecha", 0.85
        elif abs(dec(prev.get("total", 0.0)) - total) <= dec(0.01):
            tipo, similitud = "monto_proveedor", 0.60

        if tipo is None:
            continue

        coincidencias.append(
            {
                "id_factura_previa": prev.get("id_factura", "?"),
                "tipo_coincidencia": tipo,
                "similitud": similitud,
                "fecha_registro_previa": prev.get("fecha_registro", "?"),
                "estado_pago_previa": prev.get("estado_pago", "desconocido"),
            }
        )

    # Solo las coincidencias fuertes son hallazgo. 'monto_proveedor' es una
    # pista que puede ser perfectamente legitima (un abono mensual fijo), asi
    # que se reporta como dato pero no como desviacion.
    for c in coincidencias:
        if c["tipo_coincidencia"] == "exacta":
            desviaciones.append(
                catalogo.desviacion(
                    "FRA-DUP-001",
                    "id_factura",
                    f"El comprobante {f.id_factura} ya figura registrado "
                    f"(estado: {c['estado_pago_previa']}, registrado el "
                    f"{c['fecha_registro_previa']}). Pagarlo de nuevo seria "
                    f"un doble pago de {f.moneda} {flt(total):,.2f}",
                    declarado=f.id_factura,
                    monto_impactado=flt(total),
                )
            )
        elif c["tipo_coincidencia"] == "huella":
            desviaciones.append(
                catalogo.desviacion(
                    "FRA-DUP-002",
                    "id_factura",
                    f"Misma huella que {c['id_factura_previa']}: coinciden RUC, "
                    f"serie, numero, total ({flt(total):,.2f}) y fecha de "
                    f"emision ({f.fecha_emision})",
                    declarado=mi_huella,
                    monto_impactado=flt(total),
                )
            )
        elif c["tipo_coincidencia"] == "monto_fecha":
            desviaciones.append(
                catalogo.desviacion(
                    "FRA-DUP-003",
                    "total",
                    f"Mismo monto ({flt(total):,.2f}) y misma fecha de emision "
                    f"({f.fecha_emision}) que {c['id_factura_previa']} "
                    f"(estado: {c['estado_pago_previa']})",
                    declarado=f"{flt(total):,.2f} / {f.fecha_emision}",
                    monto_impactado=flt(total),
                )
            )

    # --- cuenta bancaria: la senal mas importante ---------------------------
    proveedor = repositorio.buscar_proveedor(f.ruc_proveedor)
    cuenta_declarada_mask = enmascarar_cuenta(
        f.cuenta_bancaria.cci or f.cuenta_bancaria.numero_cuenta
    )
    cuenta_coincide = False
    cuenta_registrada_mask: str | None = None

    if proveedor is None:
        # Sin maestro no se puede comparar. No es un hallazgo de esta
        # herramienta: lo reporta `analizar_proveedor_contrato` como PRV-MAE-001.
        cuenta_coincide = False
        cuenta_registrada_mask = None
    else:
        cuenta_coincide, cci_reg = _cuentas_coinciden(f, proveedor.cuentas_registradas)
        cuenta_registrada_mask = enmascarar_cuenta(cci_reg)

        if not cuenta_coincide:
            desviaciones.append(
                catalogo.desviacion(
                    "FRA-CTA-004",
                    "cuenta_bancaria.cci",
                    f"La cuenta de abono declarada ({f.cuenta_bancaria.banco}, "
                    f"{cuenta_declarada_mask}) no coincide con ninguna de las "
                    f"{len(proveedor.cuentas_registradas)} cuenta(s) registradas "
                    f"del proveedor (la registrada es {cuenta_registrada_mask}). "
                    f"Patron tipico de fraude de desvio de pago",
                    declarado=cuenta_declarada_mask,
                    esperado=cuenta_registrada_mask,
                    monto_impactado=flt(total),
                )
            )

        # titular vs razon social: comparacion laxa (mayusculas, puntuacion)
        def _norm(s: str) -> str:
            return "".join(c for c in s.upper() if c.isalnum())

        if _norm(f.cuenta_bancaria.titular) != _norm(proveedor.razon_social):
            desviaciones.append(
                catalogo.desviacion(
                    "FRA-CTA-005",
                    "cuenta_bancaria.titular",
                    f"El titular de la cuenta ('{f.cuenta_bancaria.titular}') no "
                    f"corresponde a la razon social registrada "
                    f"('{proveedor.razon_social}')",
                    declarado=f.cuenta_bancaria.titular,
                    esperado=proveedor.razon_social,
                    monto_impactado=flt(total),
                )
            )

    # --- proveedor nuevo ----------------------------------------------------
    dias_alta = dias_desde(proveedor.fecha_alta) if proveedor else None
    proveedor_nuevo = bool(
        proveedor
        and (
            proveedor.estado == "nuevo"
            or (dias_alta is not None and dias_alta <= config.DIAS_PROVEEDOR_NUEVO)
        )
    )

    if proveedor_nuevo and flt(total) > config.MONTO_ALTO_PROVEEDOR_NUEVO:
        desviaciones.append(
            catalogo.desviacion(
                "FRA-NUE-006",
                "ruc_proveedor",
                f"Proveedor dado de alta hace {dias_alta} dias "
                f"(estado '{proveedor.estado if proveedor else '?'}') facturando "
                f"{f.moneda} {flt(total):,.2f}, por encima del umbral de "
                f"S/ {config.MONTO_ALTO_PROVEEDOR_NUEVO:,.2f} para proveedores nuevos",
                declarado=f"alta {proveedor.fecha_alta if proveedor else '?'} ({dias_alta} dias)",
                monto_impactado=flt(total),
            )
        )

    # --- monto justo por debajo del umbral de aprobacion -------------------
    umbral = dec(config.UMBRAL_APROBACION)
    margen = redondear(umbral * dec(config.MARGEN_SPLIT_PCT) / dec(100))
    piso = redondear(umbral - margen)
    distancia = redondear(umbral - total)
    bajo_umbral_sospechoso = piso <= total < umbral

    if bajo_umbral_sospechoso:
        desviaciones.append(
            catalogo.desviacion(
                "FRA-UMB-007",
                "total",
                f"Total {f.moneda} {flt(total):,.2f} esta S/ {flt(distancia):,.2f} "
                f"({flt(redondear(distancia / umbral * dec(100))):.2f}%) por debajo "
                f"del umbral de aprobacion de S/ {config.UMBRAL_APROBACION:,.2f}. "
                f"Dentro del margen de {config.MARGEN_SPLIT_PCT:.1f}% que indica "
                f"posible fraccionamiento deliberado para evitar una firma",
                declarado=f"{flt(total):,.2f}",
                esperado=f"umbral {config.UMBRAL_APROBACION:,.2f}",
                monto_impactado=flt(total),
            )
        )

    indicadores = [d["codigo"] for d in desviaciones if d["codigo"].startswith("FRA-")]

    return {
        "datos_disponibles": True,
        "confianza_insuficiente": False,
        "id_factura": f.id_factura,
        "huella": mi_huella,
        "duplicados_detectados": coincidencias,
        "cuenta_bancaria_coincide": cuenta_coincide,
        "cuenta_declarada_enmascarada": cuenta_declarada_mask,
        "cuenta_registrada_enmascarada": cuenta_registrada_mask,
        "banco_declarado": f.cuenta_bancaria.banco,
        "proveedor_en_maestro": proveedor is not None,
        "proveedor_nuevo": proveedor_nuevo,
        "dias_desde_alta_proveedor": dias_alta,
        "monto_bajo_umbral_sospechoso": bajo_umbral_sospechoso,
        "distancia_al_umbral": flt(distancia) if bajo_umbral_sospechoso else None,
        "umbral_aprobacion": config.UMBRAL_APROBACION,
        "indicadores_fraude": indicadores,
        "desviaciones": desviaciones,
    }
