# Guía de traspaso — dashboard-whaticket

Todo lo necesario para que otra persona retome el proyecto: qué se usa del ETL, cómo
se relacionan, cómo se sacan los números, por qué falta el 4.º cuadro y cómo optimizar
las queries pensando en un **sistema de extracción continua**.

> Fuentes de verdad: el análisis original está en `../ANALISIS_REDES_documentacion.md`
> (fuera del repo). La propuesta del 4.º cuadro, en `docs/propuesta-cuadro-conversion.md`.

---

## 1. Modelo mental en una frase

El dashboard **NO tiene base propia**: lee en vivo la **misma Postgres que llena el ETL
de Whaticket** (contenedor `etlwhaticket-db-1`, compartida 24/7). Sobre esos datos hace
dos cosas: (a) agrega **cuadros deterministas** con SQL, y (b) un **worker de scoring**
que corre un LLM por conversación y escribe su única tabla propia, `conversation_scores`.

`datos` y `sistemas` son **dos cuentas que conviven en la MISMA base** (columna
`account`). **Toda** lectura va scopeada por `account`.

---

## 2. Qué se usa del ETL (tablas leídas)

| Tabla | Para qué la usa el dashboard | Campos clave |
|---|---|---|
| `conversations` | unidad base (una "visita" del cliente) | `id`, `account`, `ticket_id`, `queue_id`, `connection_id`, `user_id` (suele venir NULL), `created_at`, `resolved_at`, `is_new_contact` |
| `messages` | detección de depósito, atribución de operador, transcript | `conversation_id`, `account`, `from_me`, `is_note`, `sent_from` (CHATBOT/humano), `user_id`, `media_type`, `body`, `created_at` |
| `tickets` | agrupar visitas y traer canal/cliente | `id`, `contact_id`, `channel` |
| `contacts` | nombre/número del cliente | `id`, `name`, `number` |
| `users` | **nombre del operador** (fuente canónica) | `id`, `name` |
| `queues` | **segmento de negocio** (por nombre de cola) | `id`, `name`, `account` |
| `connections` | canal de la conversación | `id`, `channel` |

**Tabla propia (la escribe el dashboard, no el ETL):** `conversation_scores`.

---

## 3. Relaciones con el ETL

- `tickets` **1—\*** `conversations`: un ticket tiene varias "visitas" separadas en el
  tiempo (~3,5 conv/ticket promedio). El scorer usa las otras visitas del ticket como
  **contexto** para no juzgar un fragmento a ciegas (`src/context.py`).
- `conversations` **1—\*** `messages` (`messages.conversation_id`).
- `conversations.queue_id` → `queues` (segmento) · `conversations.connection_id` →
  `connections` (canal) · `conversations.user_id` **suele ser NULL** (por eso el operador
  se reconstruye desde `messages`, ver §4).
- `messages.user_id` → `users` (operador real) · `tickets.contact_id` → `contacts`.
- **Frontera de responsabilidad:** el ETL llena todas las tablas de arriba en continuo;
  el dashboard **solo lee** de ellas y **solo escribe** `conversation_scores`. El worker
  vive en el mismo contenedor que la API (`src/app.py` lo arranca en un thread si
  `SCORING_ENABLED`), pero puede correrse aparte con `scripts/run_scoring.py`.
- `users`/`queues` vienen **pobladas en producción** (el monitor del ETL las llena). En
  local estaban vacías y hubo que seedearlas (`scripts/seed_users.py`, `seed_queues.py`);
  sin `users`, el nombre del operador cae al fallback de firma.

---

## 4. Cómo se sacan los números

### Operador de una conversación
`conversations.user_id` suele ser NULL → el operador se define como el `messages.user_id`
con **más mensajes de negocio** en la conversación (`src/metrics.py::primary_operator`).
El nombre sale por JOIN a `users` (`COALESCE(u.name, cs.user_name)`); el fallback
`user_name` es la firma `*Nombre:*` del cuerpo (`src/operators.py`).

### Segmento
`src/segments.py::segment_for_queue(nombre_de_cola)` → `jugador | agente | marketing |
interno | descartar | otro`, por substring normalizado (ej. `onlysorti/modosorti/sortigo/
jugador` → jugador; `prueba/test` → descartar). **La segmentación es por cola, no por
cuenta** (una cuenta puede tener jugadores y agentes).

### Depósito (determinista — el "depósito" del análisis)
No es monto ni un campo de la plataforma (esos vienen **vacíos**: `isAdConversion`,
`convertedToSale`, `csat` — bug de config del ETL, ver §5.1 del análisis). Se **infiere**:
una conversación **tiene depósito** si el cliente mandó una **imagen** (`media_type ILIKE
'%image%'` con `from_me=false`) **Y** la conversación tiene **contexto de recarga**
(`body ~* 'recarg|comprobante|dep[oó]sit|transferenc'`, `src/deposits.py::RECHARGE_PATTERN`).
`veces` = cantidad de comprobantes (imágenes del cliente) en conversaciones con contexto.

### Los 3 cuadros (todos segmento jugador, full-scale sobre `conversations`, ventana móvil)
1. **Nuevos jugadores vs % depósito** (`new_vs_deposit_by_month`): `nuevos` = conversaciones
   con `is_new_contact`; `%depósito` = con_dep/conv por mes.
2. **% depósito WhatsApp por operador** (`deposit_pct_by_operator`): solo canal WHATSAPP;
   meses con **<8 conversaciones se omiten** (evita % ruidoso); top-7 operadores + "Otros".
3. **Carga mensual por operador** (`load_by_operator`): conversaciones/mes por operador;
   top-7 + "Otros".

### Scoring de calidad (`conversation_scores`, vía LLM)
El worker (`src/worker.py`) toma conversaciones **resueltas y aún no scoreadas** (newest-first,
por cuenta), es **idempotente** (`NOT EXISTS` en `conversation_scores`) y crash-safe. Por
conversación: `router` decide **rúbrica** (`human`/`bot` según quién atendió) y
**elegibilidad** (`evaluated`/`skipped`); si es evaluable, el LLM lee la conversación +
contexto del hilo y devuelve `dimensions` (empatía/claridad/resolución/tono) + `rating_label`.
La **estrella (1-5) es traducción DETERMINISTA** de la etiqueta (`src/rubrics.py`), **el LLM
nunca decide el número**. Se guarda también `deposit_count` (gate determinista, independiente
del scoring).

> **Clave:** el scoring mide **CALIDAD DE ATENCIÓN**, no conversión. Es otro eje (ver §5).

---

## 5. Por qué NO se pudo hacer el 4.º cuadro (`evolucion_operadores`)

El 4.º gráfico canónico del análisis grafica, por operador y por mes, **% conversión**
(verde) y **% atención pasiva** (roja). Esos dos números salen de una clasificación LLM
**distinta** (`analizar_grande.py`, prompt de conversión que devolvía `resultado` ∈
{cuenta_creada, recarga_confirmada, …}, `enganche`, `calidad_atencion` ∈ {empujó, **pasivo**,
no_respondió}).

- **% conversión** = `resultado` ∈ {cuenta_creada, recarga_confirmada}.
- **% atención pasiva** = `calidad_atencion` == `paso_pasivo`.

**No es reproducible con lo que hay, por dos motivos independientes:**

1. **Otro eje.** El scorer del dashboard mide calidad (estrellas), nunca preguntó si el
   jugador convirtió ni si el operador empujó/fue pasivo. La columna `resultado` de
   `conversation_scores` existe pero está **vacía**; no hay señal de `calidad_atencion`.
   Un operador puede tener 5 estrellas de calidad y ser pasivo en conversión.
2. **Cobertura ínfima.** Los otros 3 cuadros son full-scale (SQL sobre todas las
   conversaciones). El 4.º dependería del LLM, y hoy el scoring cubre una fracción mínima:

   | Cuenta | Jugador total | Scoreadas | Cobertura |
   |---|---|---|---|
   | sistemas | 36.287 | 169 | 0,5 % |
   | datos | 3.494 | 319 | 9 % |

   El análisis tenía n≈800+ por operador; con lo scoreado hoy serían ~2-3 conv/operador-mes
   = ruido.

**Qué hace falta:** un **subsistema de clasificación de conversión full-scale** (clon de
`analizar_grande.py`: prompt simple, `format:json`, tabla propia), corriendo sobre las ~40k
conversaciones jugador (~30 h de LLM). Detalle en `docs/propuesta-cuadro-conversion.md`.

**Novedad relevante:** existe un contenedor Ollama compartido en
`https://ollama-internal.zgames.store` (vía Cloudflare, requiere token) que corre
**`qwen3:14b`** — el modelo exacto del análisis. Eso hace el batch más viable, PERO:
- `src/llm.py` **hoy no manda token de auth** ni hay `OLLAMA_TOKEN`/`USE_OPENAI` en
  `src/config.py`. Para usar ese servidor falta agregar el header `Authorization` + config.
- Migrar el scoring de calidad de `qwen3.5:4b` (actual) a `qwen3:14b` implicaría
  **re-scorear** lo ya hecho: mezclar modelos engaña al comparar (caveat §10 del análisis).
- ⚠️ El token es un **secreto**: va por env/secreto de EasyPanel, nunca al repo. Si se
  compartió en claro, **rotarlo**.

---

## 6. Optimización de queries — pensando en extracción continua

`/api/charts` es la parte cara (agrega sobre `messages`, ~2 M filas / 1,5 GB). Historial de
un cuelgue real y sus causas en el commit `dc8f822`. Estado actual y recomendaciones:

### Ya aplicado
- **Índice** `idx_messages_account_conv (account, conversation_id)` — sin él, cada query
  hacía seq scan de 2 M filas. Se asegura solo al arrancar (`ensure_indexes()` en
  `src/app.py`, `CREATE INDEX CONCURRENTLY IF NOT EXISTS`).
- **`plan_cache_mode=force_custom_plan`** en las conexiones de la API: `account` tiene 2
  valores → el plan genérico ignoraba el índice y hacía seq scan; el plan custom lo usa.
- **`statement_timeout=20s`** por conexión: un timeout del cliente **no cancela** la query
  en Postgres; sin ceiling, las huérfanas se apilan y ahogan la DB.
- **`MATERIALIZED`** en los CTE que escanean `messages` y se consumen por `LEFT JOIN` (si no,
  PG los re-ejecuta por conversación → nested loop de minutos).
- **Ventana móvil** (`CHARTS_WINDOW_MONTHS`, default 12): recorta los **meses mostrados**,
  anclada al mes más reciente de la cuenta.

### Pendiente / recomendado (orden de impacto)
1. **Acotar el scan de `messages` a la ventana.** Hoy la ventana recorta lo que se MUESTRA,
   pero los CTE todavía escanean **todos** los mensajes de la cuenta. Falta bajar el filtro
   de fecha a los CTE (menos I/O, render más liviano). Era el próximo paso planificado.
2. **Precomputar rollups (LA jugada para extracción continua).** En vez de escanear 2 M
   mensajes en cada request, mantener una tabla de agregados por `(account, operador, mes)`
   —conteos de conversaciones, con_depósito, nuevos, flag WhatsApp— **actualizada de forma
   incremental** a medida que el ETL ingesta (vista materializada con refresh programado, o
   una rollup mantenida por trigger/job). Los cuadros leerían una tabla diminuta; se elimina
   el full-scan. Idealmente, precomputar `has_deposit` por conversación **una vez** al
   ingestar (columna en `conversations` o tabla lateral), no re-evaluar el regex sobre
   `messages` en cada request.
3. **Índices de apoyo:** `conversations(account, created_at)` (para el ancla de la ventana +
   filtro de cola) y evaluar un índice parcial `messages(account) WHERE media_type ILIKE
   '%image%'` para la detección de depósito.
4. **Pool de conexiones:** hoy `_conn()` abre una conexión nueva por request. Con carga
   continua, un pool (pgbouncer o psycopg_pool) reduce overhead.

---

## 7. Config relevante (`src/config.py`, por env)

| Env | Default | Qué controla |
|---|---|---|
| `DATABASE_URL` | localhost | Postgres del ETL (compartida) |
| `OLLAMA_URL` / `OLLAMA_MODEL` | localhost / `qwen3.5:4b` | servidor + modelo del scoring |
| `SCORING_ENABLED` | false | arranca el worker en el contenedor |
| `SCORING_ACCOUNTS` | `sistemas,datos` | cuentas que scorea el worker |
| `CHARTS_WINDOW_MONTHS` | 12 | meses mostrados en los cuadros |

Falta (para el Ollama compartido, ver §5): `OLLAMA_TOKEN`, `USE_OPENAI`, y el soporte en
`src/llm.py`.

### Cómo se cargan las variables
`config.py` hace `load_dotenv()` y después lee de `os.environ`. La lista completa de
variables (con valores de ejemplo) vive en **`.env.example`** — es la fuente única.
- **Local:** copiás `.env.example` a `.env` y completás valores.
- **Prod (EasyPanel):** las definís en el panel de despliegue. Las variables del entorno
  **tienen precedencia** sobre el `.env` (`load_dotenv` no las pisa).

Los **secretos** (ej. `OLLAMA_TOKEN`) van por el `.env` local / el panel de EasyPanel,
nunca al repo (el `.env` está en `.gitignore`).

---

## 8. Flujo de despliegue y arranque (BD de prod SIN scores, solo ETL)

**T=0 · Arranque del contenedor (`lifespan`):**
1. `ensure_indexes()` (thread) crea `idx_messages_account_conv` con `CONCURRENTLY IF NOT
   EXISTS` sobre `messages` de prod. Primera vez: lo construye (tarda según el tamaño de
   `messages`, **sin bloquear** al ETL). Log: `índice asegurado: idx_messages_account_conv`.
2. Si `SCORING_ENABLED=true`, arranca el worker en otro thread.
3. La API responde de inmediato.

**T=0 · Qué se ve:** `/api/accounts` lee de `conversation_scores` (tabla del dashboard). En
prod está **vacía** → el selector viene vacío y el front muestra **"No hay datos scoreados
todavía."**. Como los cuadros se piden recién al elegir cuenta, **el dashboard arranca en
blanco**.

**El worker llena `conversation_scores` de a poco:** toma conversaciones **resueltas y no
scoreadas**, **más nuevas primero**, de a `SCORING_BATCH_SIZE` (20) por cuenta; LLM →
estrella determinista → `upsert`; idempotente (`NOT EXISTS`); cuando no hay pendientes,
duerme `SCORING_POLL_SECONDS`. Apenas entra la **primera** conversación scoreada, la cuenta
aparece y el dashboard cobra vida.

**Detalle a favor:** los 3 cuadros son **full-scale sobre datos del ETL** — no dependen de
cuánto scoreaste. Apenas hay ≥1 cuenta con score, **los cuadros muestran los datos
completos de una**. Lo que crece de a poco es la lista de scores (tickets/KPIs/distribución).

**Prerrequisitos — sin esto el dashboard queda en blanco permanente:**
1. `SCORING_ENABLED=true` (si no, el worker nunca corre → `conversation_scores` vacía).
2. `DATABASE_URL` → Postgres del ETL de prod (NO la `db` del compose, que está vacía).
3. Ollama accesible **con auth**. ⚠️ Si es el compartido con token, `src/llm.py` **hoy no
   manda el token** → el pre-flight falla, cada score da error, la tabla nunca se llena.
   Ese soporte hay que agregarlo ANTES (ver §5).

---

## 9. Concurrencia con el ETL: ¿es un problema?

La BD la escribe el ETL 24/7, el dashboard escribe scores y las queries de cuadros leen
fuerte. Análisis:

1. **Locks — NO es problema.** MVCC: los `SELECT` del dashboard no bloquean los `INSERT` del
   ETL ni al revés. `conversation_scores` es tabla propia (solo la escribe el worker) → sin
   conflicto de escritura con el ETL.
2. **I/O — competencia, hoy acotada.** Las queries de cuadros compiten por disco con las
   escrituras del ETL. Bajo control con índice + `statement_timeout` + TTL. A escala continua,
   el riesgo es escanear un `messages` creciente en cada request → **rollups** (§6) lo elimina.
3. **⚠️ Problema latente REAL — el worker mantiene una transacción abierta durante el LLM.**
   En `score_and_store` (`src/worker.py`), los `SELECT` de lectura abren transacción (psycopg
   no es autocommit) y queda **`idle in transaction` los ~7-120s que dura la llamada al LLM**,
   hasta el `commit`. En una BD escrita 24/7, una transacción vieja abierta **frena al
   autovacuum** (no limpia tuplas muertas más nuevas que ese snapshot) → **bloat** y
   degradación con el tiempo. **Fix recomendado:** conexión del worker en `autocommit=True`
   (o commitear las lecturas antes de llamar al LLM). Cambio chico, hygiene grande en una BD
   ocupada.
