-- =============================================================================
-- conversation_scores — resultado de la evaluacion por CONVERSACION
-- =============================================================================
-- Grano = conversacion (NO ticket). Convive con la BD del ETL sin tocar sus
-- tablas: solo agrega esta. Idempotente: UPSERT por conversation_id.
--
-- Capas que llenan esta tabla:
--   1) Router de elegibilidad (SQL): decide rubric + eval_status/skip_reason.
--   2) Metricas objetivas (SQL, deterministas): tiempos, conteos, was_unassigned.
--   3) LLM (solo si eval_status='evaluated'): dimensions (jsonb) + stars.
--
-- Toda conversacion queda como una fila (incluidas las no-evaluadas, con su
-- skip_reason) para que el dashboard explique la cobertura sin dañar estadistica.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS conversation_scores (
    conversation_id         uuid PRIMARY KEY,   -- soft ref -> conversations(id)
    account                 text NOT NULL,      -- 'sistemas' | 'datos'
    ticket_id               uuid,
    segment                 text,               -- jugador|agente|marketing|interno|otro
    queue_name              text,
    channel                 text,               -- WHATSAPP|FACEBOOK|INSTAGRAM
    user_id                 uuid,               -- agente/operador
    user_name               text,
    conversation_created_at timestamptz,
    resolved_at             timestamptz,

    -- --- Elegibilidad (router SQL, antes de gastar LLM) ---
    rubric                  text NOT NULL,      -- 'human' | 'bot'
    eval_status             text NOT NULL,      -- 'evaluated' | 'skipped'
    skip_reason             text,               -- 'no_customer_reply'|'no_agent_reply'|'internal_notes_only'|'anomalous_size'|'media_only' (NULL si evaluated)

    -- --- Metricas OBJETIVAS (SQL, deterministas) ---
    first_response_seconds  numeric,            -- created_at -> first_sent_message_at
    resolution_seconds      numeric,            -- created_at -> resolved_at
    message_count           integer,
    agent_message_count     integer,            -- negocio humano (sent_from<>CHATBOT)
    bot_message_count       integer,            -- negocio bot (sent_from=CHATBOT)
    contact_message_count   integer,
    was_unassigned          boolean,            -- conversations.user_id NULL (flag de asignacion whaticket)

    -- --- Scoring SEMANTICO (LLM) ---
    -- dimensions: notas por rubrica (claves distintas segun rubric).
    --   human -> {empatia, claridad, resolucion, tono, errores:[...]}
    --   bot   -> {cobertura_info, capacidad_enganche, derivacion, errores:[...]}
    dimensions              jsonb,
    llm_model               text,               -- trazabilidad (ej. qwen3.5:4b)

    -- --- Calificacion CUALITATIVA (lo que emite el LLM) ---
    -- El LLM NO inventa numeros: elige una etiqueta segun criterios de la rubrica
    -- y justifica. Es lo primario y lo visual del dashboard.
    --   human -> 'excelente'|'buena'|'aceptable'|'deficiente'|'mala'
    --   bot   -> 'optima'|'funcional'|'mejorable'|'falla'
    rating_label            text,               -- la calificacion (NULL si skipped)
    rating_rationale        text,               -- el "porque" definido de esa calificacion

    -- --- Resultado de NEGOCIO (separado de la estrella) ---
    resultado               text,               -- recarga_confirmada|... (NO entra en stars)
    deposit_count           integer,            -- comprobantes de recarga detectados (VECES, no monto); NULL si no scoreado

    -- --- La estrella (ESTIMACION) ---
    -- Traduccion DETERMINISTICA de rating_label (tabla que controlamos), NO salida del LLM.
    stars                   numeric,            -- 1..5, NULL si eval_status='skipped'
    stars_breakdown         jsonb,              -- como se mapeo la etiqueta -> estrella (transparencia)

    is_estimate             boolean NOT NULL DEFAULT true,
    scoring_version         text,               -- version de la rubrica
    scored_at               timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chk_rubric      CHECK (rubric IN ('human', 'bot')),
    CONSTRAINT chk_eval_status CHECK (eval_status IN ('evaluated', 'skipped')),
    -- coherencia: skipped => sin estrella y con razon; evaluated => con estrella y sin razon
    CONSTRAINT chk_eval_coherence CHECK (
        (eval_status = 'skipped'   AND stars IS NULL     AND skip_reason IS NOT NULL) OR
        (eval_status = 'evaluated' AND skip_reason IS NULL)
    ),
    CONSTRAINT chk_stars_range CHECK (stars IS NULL OR (stars >= 1 AND stars <= 5))
);

-- Idempotente para BD ya creada (el CREATE ... IF NOT EXISTS no agrega columnas).
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS deposit_count integer;

CREATE INDEX IF NOT EXISTS idx_scores_account_segment ON conversation_scores (account, segment);
CREATE INDEX IF NOT EXISTS idx_scores_user            ON conversation_scores (user_id);
CREATE INDEX IF NOT EXISTS idx_scores_created         ON conversation_scores (conversation_created_at);
CREATE INDEX IF NOT EXISTS idx_scores_rubric_status   ON conversation_scores (rubric, eval_status);

COMMIT;
