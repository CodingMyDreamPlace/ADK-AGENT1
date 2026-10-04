# Arquitectura productiva — AP Ops

> Documento complementario a [PLAN-AP-OPS.md](PLAN-AP-OPS.md).
> El plan describe **qué construir y en qué orden**. Este documento describe
> **dónde corre y con qué se integra** cuando deja de ser un demo local.

---

## 1. Alcance

El sistema hace triaje autónomo de excepciones de facturas de proveedores
(Cuentas por Pagar): recibe una factura, la valida contra la orden de compra, la
recepción, las reglas tributarias, el histórico de pagos y el maestro de
proveedores; emite hallazgos tipados, un plan de acciones con responsable y SLA,
y una decisión auditable.

El KPI que lo justifica es el **% de facturas *touchless***: facturas procesadas
sin intervención humana. El analista deja de investigar y pasa a decidir.

---

## 2. Principio rector

> **El agente no es el sistema de registro. El ERP lo es.**

Esta restricción define todas las demás decisiones:

- El agente **lee** del ERP: órdenes de compra, notas de recepción, maestro de
  proveedores, histórico de pagos.
- El agente **escribe** a su propio almacén (BigQuery): expedientes, hallazgos,
  registros de auditoría.
- El agente **nunca** escribe un asiento contable ni libera un pago.

La liberación del pago la ejecuta el ERP cuando un humano aprueba en su cola de
trabajo. El agente aporta el expediente que hace que esa aprobación tome 30
segundos en lugar de 10 minutos.

Invertir esto —dejar que el agente escriba en el ERP— cambia un problema de
productividad por un problema de auditoría, y ninguna área de finanzas lo firma.

---

## 3. Diagrama

```
  FUENTES                    INGESTA                    PROCESAMIENTO
┌──────────────┐      ┌──────────────────┐      ┌─────────────────────────┐
│ correo del   │─────►│ Cloud Run (svc)  │      │                         │
│ proveedor    │      │ receptor-correo  │──┐   │  ap-ops-pipeline        │
│ (Gmail/Graph)│      └──────────────────┘  │   │  Cloud Run JOB          │
├──────────────┤      ┌──────────────────┐  │   │  (Workflow puro)        │
│ portal /     │─────►│ GCS bucket       │──┤   │                         │
│ upload       │      │ facturas-in/     │  │   │  ┌───────────────────┐  │
├──────────────┤      └────────┬─────────┘  ├──►│  │ 4 validadores     │  │
│ SFTP / EDI   │─────►         │            │   │  │ scoring           │  │
├──────────────┤               ▼            │   │  │ plan + crítico    │  │
│ ERP (lote)   │─────► ┌──────────────────┐ │   │  │ decisión (R-xx)   │  │
└──────────────┘       │ Document AI      │─┘   │  └───────────────────┘  │
                       │ Invoice Parser   │     └───────────┬─────────────┘
                       └────────┬─────────┘                 │
                                │                           │
                        ┌───────▼──────────┐                │
                        │ Pub/Sub          │                │
                        │ facturas.nuevas  │                │
                        │ + dead-letter    │                │
                        └──────────────────┘                │
                                                            │
  DATOS                                                     │
┌──────────────────────────────────────────┐                │
│ ERP (SAP / Oracle / Softland)            │◄───lectura─────┤
│   ← la VERDAD de OC, recepciones, maestro│                │
├──────────────────────────────────────────┤                │
│ BigQuery  ap_ops.*                       │◄───escritura───┤
│   expedientes, hallazgos, auditoría      │                │
├──────────────────────────────────────────┤                │
│ Cloud SQL (Postgres)                     │◄───sesiones────┤
│   sesiones ADK, estado HITL pausado      │                │
├──────────────────────────────────────────┤                │
│ GCS  artefactos/   memos de auditoría    │◄───artifacts───┤
├──────────────────────────────────────────┤                │
│ Secret Manager   credenciales ERP, keys  │◄───────────────┤
└──────────────────────────────────────────┘                │
                                                            │
  SALIDA / HUMANOS                                          ▼
┌─────────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
│ ap-ops-api          │   │ cola de trabajo  │   │ Cloud Trace         │
│ Cloud Run SERVICE   │   │ (AppSheet / web  │   │ Cloud Monitoring    │
│ (coordinador chat)  │◄──│  app / el ERP)   │   │ Looker Studio       │
│ + API Gateway       │   │  ← aprueba/rechaza│  │  ← KPIs y alertas   │
└─────────────────────┘   └──────────────────┘   └─────────────────────┘
```

---

## 4. Runtime: dos formas del mismo código

Los dos paquetes del diseño híbrido (`ap_ops/` y `ap_ops_pipeline/`) no son
duplicación: son dos perfiles de ejecución del mismo grafo.

| | `ap_ops/` — coordinador | `ap_ops_pipeline/` — Workflow |
|---|---|---|
| **Despliegue** | Cloud Run **Service** | Cloud Run **Job** |
| **Disparo** | HTTP, sincrónico | Pub/Sub, asincrónico |
| **Uso** | el analista pregunta, conversa, aprueba | procesamiento masivo |
| **Timeout** | segundos (límite de request: 60 min) | horas |
| **Escala** | a cero cuando nadie pregunta | paralelismo por tarea |
| **Entry point** | `app = App(root_agent=coordinador, ...)` | `app = App(root_agent=pipeline_ap, ...)` |

**Por qué importa la distinción:** un Cloud Run *Service* tiene límite de tiempo
por request. Un pipeline de 7 llamadas a LLM entra sin problema. Procesar las 500
facturas del cierre de mes en un solo request, no. Eso es un *Job* con
`--tasks 500 --parallelism 10`.

**Configuración:** las llamadas a LLM son I/O-bound, no CPU-bound. Conviene
`--concurrency 80` (en lugar del default conservador) y `--cpu 1 --memory 1Gi`.
En el *Service*, `--min-instances 1` evita que el analista pague el arranque en
frío, que no es despreciable porque `google-adk[gcp]` es un import grande.

---

## 5. Decisiones de arquitectura

### ADR-01 — Cloud Run sobre Agent Engine y GKE

| | **Cloud Run** | **Agent Engine** | **GKE** |
|---|---|---|---|
| Sesiones / memoria | propias (Cloud SQL) | administradas | propias |
| Tracing | se conecta (`--otel_to_cloud`) | incluido | se conecta |
| Control | total | limitado | total |
| Costo | escala a cero | sin escala a cero | cluster siempre encendido |
| Requiere Vertex | no | **sí, obligatorio** | no |
| Jobs batch | nativo | no es su fuerte | sí |
| Curva de entrada | baja | baja | alta |

**Decisión: Cloud Run.** Tres razones:

1. El cierre de mes necesita **Jobs** para batch, y eso es territorio de Cloud Run.
2. La integración con un ERP on-premise implica VPC connector o Private Service
   Connect, directo en Cloud Run.
3. La carga es *bursty* —picos al cierre, casi nada el resto—, así que escalar a
   cero es ahorro real.

Agent Engine tiene sentido si lo que se busca es el agente conversacional con
memoria de largo plazo y cero infraestructura, aceptando el sobrecosto. GKE solo
si ya existe un cluster en uso.

### ADR-02 — La decisión de pago es determinista, no del LLM

`DecisionAP` la produce `herramientas/decision.py` con una tabla de reglas
numeradas (`R-01..R-08`), y lleva `regla_aplicada` y `umbral_usado`.

Un modelo puede evaluar riesgo y proponer acciones. La regla que determina si el
dinero se mueve tiene que ser idéntica en cada corrida y reconstruible por un
auditor seis meses después. Esto no es negociable y no cambia en producción.

### ADR-03 — Un único punto de contacto con los datos

`herramientas/repositorio.py` es el único módulo que lee datos de negocio. Hoy lee
JSON de `datos/`; en producción cambia a BigQuery y/o la API del ERP. Nada por
encima de ese módulo se modifica: ni los esquemas, ni los validadores, ni el
grafo, ni los tests.

### ADR-04 — El estado tri-valor absorbe la inestabilidad externa

`EstadoValidador = conforme | observado | no_evaluable`.

Toda API externa (SUNAT, el ERP, Document AI) puede no responder. Con el tercer
estado, "no pude verificarlo" es una respuesta legítima: el validador no inventa
cifras y el pipeline no se cae. Además, `ConsolidadoValidacion.validadores_no_evaluables`
bloquea el camino *touchless* — no se aprueba sin intervención lo que no se pudo
verificar.

---

## 6. Capa de datos

### Ingesta

En Perú la vía dominante es **correo del proveedor con PDF y XML adjuntos**.

- **XML de SUNAT** (comprobante electrónico): datos estructurados y firmados.
  Es la ruta preferida — no hay OCR que fallar.
- **PDF escaneado**: **Document AI — Invoice Parser**. Devuelve *confidence* por
  campo, que alimenta `Factura.confianza_ocr` y `Factura.observaciones_ocr`, y de
  ahí el estado `no_evaluable`.

Flujo: archivo en GCS → Eventarc → Pub/Sub → Cloud Run Job. Desacoplado, con
reintentos y dead-letter queue.

### Maestros y lookups

Tres caminos, y el real es el tercero:

1. **BigQuery**, si el ERP ya replica sus maestros al warehouse. ADK trae
   `BigQueryToolset` con `WriteMode.BLOCKED`: el agente consulta pero
   físicamente no puede escribir.
2. **API REST del ERP**, cuando se necesita dato en vivo (el saldo de una OC
   cambia durante el día).
3. **Híbrido**: maestros desde BigQuery (cambian poco), saldos de OC desde la API
   del ERP (cambian siempre).

### Sesiones y estado HITL

`InMemorySessionService` **no sirve en producción**, por una razón concreta: si el
pipeline queda pausado esperando que alguien confirme un pago de S/ 45.000 y
Cloud Run escala a cero o redespliega, la invocación pausada desaparece y el pago
queda en limbo.

- **Elegido:** Cloud SQL (Postgres) + `DatabaseSessionService("postgresql+asyncpg://...")`.
  Control total, barato, y el estado es consultable con SQL.
- **Alternativa:** `VertexAiSessionService` (sesiones administradas de Agent Engine).
- **Nunca:** sqlite en `/tmp` de Cloud Run — filesystem efímero que muere con la
  instancia.

### Artefactos

`GcsArtifactService(bucket_name=...)` para los memos de auditoría en Markdown y
los registros de auditoría en JSON.

---

## 7. APIs

### Consumidas

| API | Para qué | Reemplaza en el demo |
|---|---|---|
| **Document AI** (Invoice Parser) | extraer campos del PDF | `datos/facturas/*.json` |
| **SUNAT** — consulta de RUC y validez de comprobante | verificar que el RUC exista, esté activo y habido, y que el comprobante esté declarado | el dígito verificador local |
| **ERP** (REST / SOAP) | OC, recepciones, maestro, histórico | `herramientas/repositorio.py` |
| **Gmail / Microsoft Graph** | leer el buzón de facturas | — |
| **Vertex AI** | los modelos Gemini | AI Studio |

La de SUNAT es la de mayor valor incremental: el validador tributario local solo
verifica que el RUC tenga el **formato** correcto. Un RUC puede ser
sintácticamente válido y pertenecer a una empresa de baja de oficio. La consulta
convierte una validación de formato en una validación real.

**Regla para toda API externa:** `retry_config` acotado con `exceptions`
específicas, `timeout` por nodo, y un camino explícito a `no_evaluable`.

### Expuestas

ADK incluye un servidor FastAPI:

```powershell
adk api_server ap_ops    # /run, /run_sse, /apps/{app}/users/{u}/sessions/...
```

Para producción:

- **`--with_ui` solo en desarrollo.** La UI de ADK es para desarrollo, no para
  usuarios finales.
- Detrás de **API Gateway** o **Cloud Endpoints**: autenticación, cuotas y rate
  limiting. Un endpoint de agente sin rate limit es una factura de Vertex
  esperando a pasar.
- O un FastAPI propio envolviendo `Runner`, si se quieren endpoints con la forma
  del dominio (`POST /facturas/{id}/triaje`) en lugar de la forma genérica de ADK.
- **A2A** (`adk api_server --a2a`): expone el protocolo agent-to-agent, útil si
  más adelante un agente coordinador de finanzas tiene que invocar a este.

---

## 8. Cola de trabajo humano

Un sistema que genera 40 recomendaciones por día y las deja en BigQuery no sirve
de nada. Alguien tiene que actuar. De menor a mayor esfuerzo:

1. **AppSheet** sobre la tabla de BigQuery: tabla más botones de aprobar/rechazar,
   sin código. Suficiente para arrancar.
2. **Integrar en el ERP**: el expediente como adjunto o nota en el workflow de
   aprobación que el área ya usa. Es la de mejor adopción, porque nadie cambia de
   herramienta.
3. **App propia**, si hace falta una experiencia específica.
4. **Notificaciones**: Pub/Sub → Cloud Run → correo/Teams para los casos críticos
   (`FRA-*`), que no pueden esperar a que alguien abra un dashboard.

---

## 9. Observabilidad

### Técnica (Fase 3 del plan)

- `App(plugins=[PluginAuditoriaAP()])` → `RegistroAuditoria` por invocación,
  persistido como artifact.
- OpenTelemetry → Cloud Trace (`adk ... --otel_to_cloud`), spans por `node_path`.
- Métricas: `ap_ops.hallazgos{categoria,severidad}`,
  `ap_ops.recomendaciones_ops{tipo_accion}`, `ap_ops.tokens_por_factura`,
  `ap_ops.latencia_nodo{nodo}`.
- Eje ops: toda señal (error de herramienta, tormenta de reintentos, latencia o
  tokens sobre presupuesto, desacuerdo LLM-vs-determinista) produce una
  `RecomendacionOps` con acción, responsable y runbook — no una línea de log.

### De negocio

Looker Studio sobre BigQuery: **% touchless**, tiempo de ciclo, monto retenido,
hallazgos por categoría, falsos positivos. Esto es lo que mira el gerente de
finanzas y lo que mantiene vivo al proyecto.

### Alertas (Cloud Monitoring)

- tasa de error de validadores > 5%
- costo por factura sobre presupuesto
- latencia p95
- **caída del % touchless** — la más importante: suele significar que un maestro
  quedó desactualizado y el sistema empezó a observar todo.

---

## 10. Seguridad e identidad

- **Secret Manager** para credenciales del ERP y claves. Nunca `.env` en la imagen.
- **Una service account por componente**, con el mínimo privilegio:
  - pipeline: `roles/aiplatform.user`, `roles/bigquery.dataEditor` (solo el dataset
    `ap_ops`), `roles/bigquery.jobUser`, `roles/storage.objectAdmin` (solo su bucket),
    `roles/cloudsql.client`, `roles/secretmanager.secretAccessor`
  - observabilidad: `roles/cloudtrace.agent`, `roles/monitoring.metricWriter`,
    `roles/logging.logWriter`
- **Lectura del ERP de solo lectura.** El principio rector se hace cumplir con
  permisos, no con disciplina.
- **Datos sensibles:** los expedientes llevan RUC, razón social y cuentas
  **enmascaradas** por diseño (`cuenta_declarada_enmascarada`). Decidir
  explícitamente qué se persiste en spans de OTel: ADK captura contenido en spans
  por default (`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`).
- **Migración obligatoria a ADC.** El `.env` actual usa
  `GOOGLE_GENAI_USE_ENTERPRISE=0` + `GOOGLE_API_KEY` de AI Studio, lo que bloquea
  `BigQueryToolset`, `GcsArtifactService`, `VertexAiSessionService` y
  `adk deploy agent_engine`:

  ```powershell
  gcloud auth application-default login
  gcloud config set project <proyecto>
  # GOOGLE_GENAI_USE_ENTERPRISE=1, sin GOOGLE_API_KEY
  ```

  Ojo: `cli/deployers/_cloud_run_deployer.py` fuerza
  `GOOGLE_GENAI_USE_ENTERPRISE=1` en el entorno generado. Si se mantiene la API
  key hay que sobreescribirlo con `--env GOOGLE_GENAI_USE_ENTERPRISE=0`.

---

## 11. Brechas para producción

Lo que el diseño actual **no** cubre todavía:

1. **Idempotencia.** La falla catastrófica de AP es pagar dos veces. Hace falta una
   clave de idempotencia (la `huella` de la factura) con constraint único, para que
   reprocesar el mismo mensaje no genere un segundo expediente ni una segunda
   autorización. Pub/Sub garantiza *at-least-once*, no *exactly-once*: va a
   entregar duplicados.
2. **Tope duro de costo.** Hoy los presupuestos solo *observan*. Hace falta un
   circuit breaker: si el costo por factura supera el umbral, el agente para y
   escala, no sigue gastando.
3. **Retención y PII.** Política de retención en BigQuery y decisión explícita
   sobre el contenido en trazas.
4. **Versionado del pipeline.** `ExpedienteAP.version_pipeline` está en el esquema,
   pero falta la disciplina: al cambiar un prompt o un umbral, los expedientes
   viejos tienen que seguir siendo interpretables.
5. **Shadow mode.** Antes de dejarlo decidir, correrlo 30 días en paralelo al
   proceso humano comparando decisiones. Es la única forma de medir falsos
   positivos antes de que el área pierda la confianza. Es la Fase 6 (evaluación)
   escalada a producción.

---

## 12. Impacto en el código

| Pieza | Cambio al ir a producción |
|---|---|
| `esquemas/` | **ninguno** |
| `nodos/`, `flujo.py` | **ninguno** |
| `coordinador.py` | **ninguno** |
| `herramientas/repositorio.py` | JSON → BigQuery / API del ERP |
| `agent.py` | `App(...)` con `DatabaseSessionService` y `GcsArtifactService` |
| *nuevo* `adaptadores/` | Document AI, SUNAT, ERP |
| *nuevo* `main_job.py` | entry point del Cloud Run Job que consume de Pub/Sub |

Que la lista de "ninguno" sea tan larga es el objetivo del diseño, no una
coincidencia: `repositorio.py` como único punto de contacto con los datos, y la
decisión en una tabla determinista, son precisamente las dos piezas que cambian.

---

## 13. Camino de migración

| Etapa | Qué | Depende de |
|---|---|---|
| **A** | Fases 1-3 del plan, local con fixtures | — |
| **B** | Migrar a ADC/Vertex; deploy del Service a Cloud Run | Fase 4 |
| **C** | `repositorio.py` → BigQuery; cargar maestros reales del ERP | B |
| **D** | Cloud SQL + `DatabaseSessionService`; HITL durable | Fase 5 |
| **E** | Ingesta: Document AI + GCS + Pub/Sub + Cloud Run Job | C |
| **F** | Cola de trabajo humano (AppSheet o ERP) + notificaciones | D |
| **G** | Shadow mode 30 días; dashboard de negocio | E, F |
| **H** | Habilitar decisión autónoma para el tramo *touchless* | G |

La etapa **H** es la que entrega el valor, y es la última a propósito: nadie
debería habilitar decisión autónoma sobre pagos sin los datos de la etapa G.
