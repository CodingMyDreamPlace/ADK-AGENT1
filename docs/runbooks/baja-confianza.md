# Runbook — Decision de baja confianza

**Sintoma:** El modelo declaro `confianza` < 0.6.

## Diagnostico
1. No confiar en el puntaje del modelo para esta factura.

## Accion
1. Enviar a revision humana; el puntaje determinista sigue siendo valido como referencia.

## Escalamiento
negocio_cxp
