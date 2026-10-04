"""Validacion de RUC peruano. Privado al paquete `herramientas`.

El RUC tiene 11 digitos: los 2 primeros identifican el tipo de contribuyente,
los 8 siguientes son el numero, y el ultimo es un digito verificador modulo 11.

IMPORTANTE sobre el alcance de esta validacion: comprueba el FORMATO, no la
existencia. Un RUC puede pasar este chequeo y pertenecer a una empresa dada de
baja de oficio. La verificacion real requiere la consulta de RUC de SUNAT
(etapa E en ARQUITECTURA.md). Hasta entonces, `ruc_valido=True` significa
"bien formado", no "activo y habido" — y el prompt del validador tributario
tiene que decirlo asi para que el modelo no sobre-interprete.

Esta es exactamente la clase de calculo que NO puede hacer un LLM: un algoritmo
de digito verificador tiene una sola respuesta correcta, y un modelo la acierta
aproximadamente el 90% de las veces. Un 10% de falsos positivos en validacion
de RUC es inaceptable.
"""

from __future__ import annotations

#: Pesos del modulo 11 aplicados a los 10 primeros digitos.
PESOS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)

#: Tipos de contribuyente por los dos primeros digitos.
TIPOS_RUC: dict[str, str] = {
    "10": "persona natural con negocio",
    "15": "persona natural no domiciliada",
    "17": "sucesion indivisa",
    "20": "persona juridica",
}


def digito_verificador(primeros_diez: str) -> int:
    """Calcula el digito verificador de los 10 primeros digitos del RUC."""
    suma = sum(int(d) * p for d, p in zip(primeros_diez, PESOS, strict=True))
    resto = 11 - (suma % 11)
    if resto == 10:
        return 0
    if resto == 11:
        return 1
    return resto


def validar_ruc(ruc: str) -> tuple[bool, str]:
    """Valida un RUC y devuelve (es_valido, tipo).

    `tipo` es uno de los dos primeros digitos conocidos, o 'desconocido'.
    Un RUC con prefijo no reconocido es invalido incluso si el digito
    verificador cuadra: no existe contribuyente tipo '30'.
    """
    limpio = "".join(c for c in (ruc or "") if c.isdigit())
    if len(limpio) != 11:
        return False, "desconocido"

    prefijo = limpio[:2]
    tipo = prefijo if prefijo in TIPOS_RUC else "desconocido"
    if tipo == "desconocido":
        return False, "desconocido"

    return digito_verificador(limpio[:10]) == int(limpio[10]), tipo


def completar_ruc(primeros_diez: str) -> str:
    """Devuelve el RUC completo de 11 digitos. Util para generar datos de prueba."""
    return primeros_diez + str(digito_verificador(primeros_diez))
