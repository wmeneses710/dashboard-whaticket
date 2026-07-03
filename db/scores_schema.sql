-- =============================================================================
-- conversation_scores — resultado de la evaluacion por CONVERSACION
-- =============================================================================
-- BORRADOR (Fase 2). Las columnas semanticas y la formula de estrellas se
-- cierran cuando definamos la rubrica, despues de restaurar la BD (Fase 0) y
-- ordenar el modelo de datos (Fase 1). NO es el schema final.
--
-- Grano = conversacion (NO ticket). Convive con la BD del ETL sin tocar sus
-- tablas: solo agrega esta. Idempotente: UPSERT por conversation_id.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS conversation_scores (
    conversation_id       uuid PRIMARY KEY,   -- soft ref -> conversations(id)
    account               text NOT NULL,      -- 'sistemas' | 'datos'
    ticket_id             uuid,
    segment               text,               -- jugador|agente|marketing|interno|otro
    queue_name            text,
    channel               text,               -- WHATSAPP|FACEBOOK|INSTAGRAM
    user_id               uuid,               -- agente/operador
    user_name             text,
    conversation_created_at timestamptz,
    resolved_at           timestamptz,

    -- --- Metricas OBJETIVAS (SQL, deterministas) ---
    first_response_seconds numeric,           -- created_at -> first_sent_message_at
    resolution_seconds     numeric,           -- created_at -> resolved_at
    message_count          integer,
    agent_message_count    integer,
    contact_message_count  integer,
    was_unassigned         boolean,

    -- --- Scoring SEMANTICO (LLM) — columnas TENTATIVAS, se ajustan en Fase 2 ---
    -- llm_model             text,             -- trazabilidad (ej. qwen3.5:4b)
    -- calidad_atencion      text,             -- jugador
    -- satisfaccion          text,             -- agente
    -- resuelto              text,             -- agente
    -- tono                  text,
    -- errores               jsonb,            -- faltas detectadas

    -- --- Resultado de NEGOCIO (separado de la estrella) ---
    -- resultado             text,             -- recarga_confirmada|... (NO entra en stars)

    -- --- La estrella (ESTIMACION) ---
    -- stars                 numeric,          -- 1..5
    -- stars_breakdown       jsonb,            -- como se compuso (transparencia)

    is_estimate           boolean NOT NULL DEFAULT true,
    scoring_version       text,               -- version de la rubrica
    scored_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scores_account_segment ON conversation_scores (account, segment);
CREATE INDEX IF NOT EXISTS idx_scores_user            ON conversation_scores (user_id);
CREATE INDEX IF NOT EXISTS idx_scores_created         ON conversation_scores (conversation_created_at);

COMMIT;
