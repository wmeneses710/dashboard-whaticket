# Diseño — Evaluación unificada por sesión

Cambio de fondo en cómo el dashboard-whaticket evalúa la atención: pasar la **unidad
de evaluación** de *conversación* a *sesión*, y unificar rating + pasividad +
conversión en **un solo pase LLM por sesión de entrada**, con el depósito como
**hecho determinista reconciliado**. Este documento fija el alcance, el modelo de
datos, la arquitectura del pase, el orden de las piezas y el plan de reproceso.

> Base fáctica del diseño: los hallazgos verificados sobre la copia local
> `whaticket_copia` (snapshot de prod). Números clave citados en la sección 1.

---

## 1. Contexto (todo el "antes" vive acá)

Hoy conviven **tres evaluaciones con tres fuentes, métodos y granos distintos**:

| Eje | Método | Grano (ventana) | Dónde |
|---|---|---|---|
| Calidad (rating) | LLM (rúbrica rica, dimensiones) | 1 conversación (episodio) | `conversation_scores` |
| Pasividad (empujó/pasivo/no_respondió) | LLM (prompt aparte, `passivity.py`) | conversación de **entrada** (1 episodio) | `player_conversions.attention` |
| Depósito (conversión) | Determinista (regex recarga + imagen) | **todos** los mensajes de la persona | `player_conversions.deposited` |

Consecuencias medidas sobre la copia:

- **Fragmentación de la unidad**: 130.204 conversaciones para solo 33.057 tickets;
  el 84,9 % de las conversaciones son fragmentos de un ticket. Sesionizando por
  *gap < 6 h* quedan **82.633 sesiones** (−36,5 %); con el override de cierre
  diferido del agente, **80.874**.
- **Notas incoherentes**: una misma interacción (ticket-ráfaga) se rankea de
  *deficiente(2)* a *excelente(5)* entre episodios; el promedio por operador
  medía **cuánto se fragmentaba** cada uno, no su calidad.
- **Skips fabricados**: de 10.188 skips `no_agent_reply`, **7.791 (76 %)** eran
  falsos (el agente respondió en un episodio hermano).
- **Pasividad sobre fragmento**: 17,2 % de las clasificaciones se hicieron sobre
  la conversación de entrada cuando la sesión real tenía en promedio 2,6 episodios.
- **Incoherencia pasividad↔depósito**: 16 casos `no_respondió` pero `deposited=true`
  (imposible salvo por grano distinto). Y `pasivo` convierte 19,7 % vs `empujó`
  22,8 %: la métrica casi no discrimina por el ruido de fuente/grano.

El `is_new_contact` **no** está mal catalogado (solo 15 de 27.263 contactos con más
de una marca): es un flag limpio de "primera vez".

---

## 2. Objetivo y alcance

**Objetivo**: que las tres evaluaciones salgan de la **misma fuente y el mismo grano
(la sesión)**, de modo que sean coherentes por construcción y drilleables en el
dashboard, atribuidas al **primer agente**.

**En alcance**:
1. Sesionización (unidad = sesión) materializada y recomputable.
2. Pase LLM único por sesión de entrada: rating + pasividad + observación de conversión.
3. Depósito determinista sobre grano sesión + **reconciliación** con la observación LLM.
4. Campo nuevo **convirtió a jugador** (re-engagement = ≥ 2 sesiones), atribuido al 1.º agente.
5. Persistir el **dónde** convirtió (drill-down).
6. Dashboard: agregados y drill-down sobre el grano sesión.
7. Reproceso con `SCORING_VERSION` como discriminador real.

**Fuera de alcance** (decisiones ya tomadas):
- Regla de **expansión entre sesiones** (propagar la pausa a episodios solo-cliente):
  descartada. Se verificó que las colas aisladas, scoreadas solas, dan
  *buena/excelente/aceptable*, **cero deficiente** → dejarlas partidas es llevable.
- Cambiar la ventana global de 6 h: validada (codo de densidad + lectura cualitativa).
- Que el LLM dictamine el depósito: descartado (el depósito es un hecho auditable).

---

## 3. Decisiones de diseño

- **D1 — Unidad = sesión.** Sesión = episodios del mismo `ticket_id` con
  *gap < 6 h* entre `created_at` consecutivos. **Override**: no cortar si
  *gap ≤ 48 h* **y** el episodio previo cierra con señal de pausa diferida del
  agente (regex `DEFERRED`). El `session_id` es el `first_conversation_id` de la
  sesión (uuid estable ya existente).
- **D2 — Un solo pase LLM por sesión de entrada.** Reemplaza al scorer por
  conversación **y** al pase separado de pasividad. Lee el transcript **mergeado**
  de la sesión y emite en una sola respuesta: dimensiones + `rating_label` +
  `atencion` + observación de conversión.
- **D3 — Depósito determinista-reconciliado.** El depósito sigue siendo el gate
  determinista (`deposits.py`), ahora sobre el grano sesión. Se guarda **además**
  la observación del LLM y un **flag de discrepancia** (`deposit_mismatch`) cuando
  determinista y LLM no coinciden. La métrica de conversión usa el **determinista**;
  el flag es señal de calidad de dato (caza falsos negativos del regex y
  alucinaciones del LLM).
- **D4 — Atribución al primer agente.** Todas las métricas de conversión/pasividad
  cuelgan de `player_conversions.user_id` (operador dominante de la conversación de
  entrada). El segundo agente no recibe mérito. Ya existe; no se agrega
  `converted_by`.
- **D5 — Convirtió a jugador = re-engagement.** Booleano `returned`: el contacto
  nuevo tuvo **≥ 2 sesiones**. Es un hecho determinista sobre el grano sesión; no
  usa LLM. Monótono (una vez `true`, siempre `true`).
- **D6 — `SCORING_VERSION` como discriminador.** Deja de ser constante muerta; se
  bumpea en cada cambio de prompt/rúbrica/unidad. El reproceso escribe la versión
  nueva; las filas viejas quedan distinguibles (auditoría y rollback).

---

## 4. Modelo de datos

### 4.1 Sesión

Tabla materializada `conversation_sessions` (recomputable, idempotente, self-healing
como `player_conversions`):

```
conversation_sessions(
  account               text NOT NULL,
  ticket_id             uuid NOT NULL,
  session_id            uuid NOT NULL,   -- = first_conversation_id de la sesión
  sess_no               int  NOT NULL,   -- 0,1,2... dentro del ticket
  start_at, end_at      timestamptz,
  episode_count         int,
  PRIMARY KEY (account, session_id)
)
-- + índice (account, ticket_id), (session_id)
-- mapeo episodio->sesión: conversations.id -> session_id (columna o tabla puente)
```

La sesionización corre en el worker por ticket con actividad nueva. `session_id`
estable = entry `first_conversation_id`.

### 4.2 `conversation_scores` (una fila por SESIÓN)

- Nueva columna `session_id` (= key lógica de la sesión).
- Nuevas columnas del pase unificado: `atencion` (enum empujó/pasivo/no_respondió),
  `deposit_observed` (bool, observación LLM), `conversion_intent` (opcional).
- `scoring_version` pasa a versión nueva (p. ej. `2026.08-session-v2`).
- Las filas viejas (por conversación) se conservan bajo su versión hasta archivar.

### 4.3 `player_conversions`

Columnas nuevas:
- `returned` boolean NOT NULL DEFAULT false — convirtió a jugador (≥ 2 sesiones).
- `deposit_observed` boolean — observación LLM del depósito.
- `deposit_mismatch` boolean — determinista ≠ LLM (calidad de dato).
- `return_session_id` uuid — dónde volvió (drill-down).
- `deposit_conversation_id` uuid — dónde depositó (drill-down).

`attention` pasa a alimentarse del pase unificado (sesión de entrada), misma fuente
que el rating. `user_id` (1.º agente) y `deposited` (determinista) sin cambios de
semántica.

---

## 5. Arquitectura del pase unificado

```
Entrada:  transcript MERGEADO de la sesión de entrada del jugador nuevo
          (todos los episodios de la sesión, orden cronológico global)
             │
             ▼
   ┌─────────────────────────┐        ┌───────────────────────────┐
   │  Pase LLM único          │        │  Gate determinista         │
   │  (extiende scorer.py)    │        │  (deposits.py, grano sesión)│
   │  emite:                  │        │  emite: deposited (hecho)   │
   │   - dimensions + label   │        └────────────┬──────────────┘
   │   - atencion             │                     │
   │   - deposit_observed     │                     │
   └───────────┬──────────────┘                     │
               │            ┌─────── reconciliación ─┘
               ▼            ▼
        conversation_scores + player_conversions
        (deposited = determinista; deposit_mismatch = det ≠ LLM)
```

- El *system prompt* del scorer se extiende para pedir también `atencion` y
  `deposit_observed`, reusando las reglas ya endurecidas (media ilegible, abandono
  del cliente, cierre no-scoreable, tono).
- El schema de salida (`build_output_schema`) suma `atencion` (enum) y
  `deposit_observed` (bool). La validación (`_validate`) los exige.
- `classify_passivity_batch` **se elimina**: la pasividad sale del mismo pase.
- La estrella sigue siendo determinista desde la etiqueta (`label_to_stars`).
- El router de elegibilidad corre sobre las stats de la **sesión** mergeada (mata
  los skips fabricados).

---

## 6. Piezas en orden de dependencia

1. **Sesionización en el worker** — `conversation_sessions` + regla D1
   (gap 6 h + override). Bloquea a todo lo demás.
2. **Router + stats sobre sesión** — `message_stats`/`decide_eligibility` sobre el
   transcript mergeado; elimina skips fabricados.
3. **Pase LLM unificado** — extender `prompts.py`/`scorer.py`/schema; borrar el pase
   de pasividad separado.
4. **Depósito grano sesión + reconciliación** — `deposits.py` sobre la sesión;
   `deposit_observed`/`deposit_mismatch` en `player_conversions`.
5. **Campo `returned` + `dónde convirtió`** — refresh de `player_conversions` con
   conteo de sesiones y los ids de retorno/depósito.
6. **Dashboard** — agregados y drill-down sobre el grano sesión (queries + front).
7. **Reproceso** — bump `SCORING_VERSION`, backfill (sección 7).

---

## 7. Plan de reproceso

1. Deploy del código con migraciones idempotentes (crean/alteran tablas al arrancar,
   patrón `ensure_indexes`/`ensure_table`).
2. Bump `SCORING_VERSION` a la versión sesión.
3. Backfill incremental (worker): re-sesionizar todos los tickets → re-scorear por
   **sesión de entrada** (~80,9 k unidades vs 130 k conversaciones) → recomputar
   `player_conversions` con `returned` + reconciliación.
4. Dashboard lee la versión nueva; filtrar por `scoring_version` permite comparar
   viejo vs nuevo y **rollback** (las filas viejas se conservan).

**Costo**: ~80,9 k sesiones. Además el pase unificado hace **una** llamada LLM donde
antes había **dos** (rating + pasividad), así que el trabajo LLM cae de forma neta.
Estimación gruesa: entre ~3,5 días (a ~16/min, ritmo observado en el backfill inicial)
y ~10 días (a 5,5/min). Se corre incremental y se monitorea.

---

## 8. Resultado esperado y validación

| Métrica de validación | Hoy | Esperado |
|---|---|---|
| Contradicciones `no_respondió` + `deposited` | 16 | **0** (misma fuente/grano) |
| Pasividad juzgada sobre fragmento | 17,2 % | **0 %** (sesión completa) |
| Skips `no_agent_reply` fabricados | 76 % | ~0 (la sesión los absorbe) |
| Pases LLM (rating + pasividad) | 2 | **1** |
| Discriminación `empujó` vs `pasivo` en conversión | 22,8 % vs 19,7 % | brecha clara |
| Drill-down jugador → sesión → rating+pasiv.+convirtió+depositó | imposible | disponible |

Criterio de éxito: el cuadro conversión-vs-pasividad **discrimina** (pasivo convierte
claramente menos que empujó), no hay contradicciones de fuente, y cada jugador
convertido es trazable a su sesión de entrada y su primer agente.

---

## 9. Riesgos y mitigaciones

- **Salida LLM más compleja** (una respuesta con rating + pasividad + depósito): un
  output malformado falla los tres ejes. → Mantener el fallback de dos niveles de
  `llm.py`; validar cada sub-objeto; reintentar por eje faltante.
- **Churn de asignación de sesión** al llegar episodios nuevos. → Recomputar por
  ticket con actividad; `session_id` = entry conversation (estable).
- **Falsos negativos del depósito determinista** (depósitos solo-texto, sin imagen).
  → El `deposit_mismatch` los **surface**; se evalúa aparte, no se auto-confía en el LLM.
- **Costo/tiempo del reproceso**. → Incremental, monitoreado, con versión vieja
  retenida para rollback.

---

## 10. Apéndice — qué se descartó y por qué

- **Expansión entre sesiones**: colas aisladas dan *buena/excelente*, 0 deficiente
  → llevable.
- **Ventana ≠ 6 h**: 6 h es el codo de densidad; lectura cualitativa lo confirmó.
- **Depósito por LLM**: es un hecho auditable; se mantiene determinista + reconciliado.
- **`converted_by` (2.º agente)**: el mérito es del 1.º; no se persiste el 2.º.
