# Runbook — Validador no evaluable

**Sintoma:** Un validador no pudo concluir. La factura NO puede resolver touchless (regla R-01).

## Diagnostico
1. Causas: confianza OCR < minimo, maestro sin leer, factura sin OC.
2. Ver `resumen` del validador: debe decir exactamente que falto.

## Accion
1. Resolver el insumo (re-escanear, completar maestro) y reprocesar.

## Escalamiento
negocio_cxp
