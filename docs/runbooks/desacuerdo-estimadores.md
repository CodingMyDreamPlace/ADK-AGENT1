# Runbook — Desacuerdo entre estimadores

**Sintoma:** El puntaje del modelo y el `puntaje_determinista` difieren en mas de 20 puntos.

## Diagnostico
1. Leer `justificacion_desacuerdo` en la evaluacion de riesgo.
2. No promediar: uno de los dos estimadores se equivoco.

## Accion
1. Un analista revisa la factura y decide cual tiene razon.
2. Si el modelo estaba equivocado de forma sistematica, ajustar el prompt del scoring.

## Escalamiento
negocio_cxp
