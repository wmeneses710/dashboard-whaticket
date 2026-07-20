-- Tabla de SESIONES (unidad de evaluacion = sesion, decision D1 del diseno).
--
-- Una sesion son los episodios (conversations) del mismo ticket_id con gap < 6h
-- entre created_at consecutivos. OVERRIDE por cierre diferido: no se corta si el
-- gap es <= 48h y el ultimo mensaje del agente del episodio previo cierra con una
-- senal de pausa diferida (regex DEFERRED en src/sessions.py). La materializa un
-- pase DETERMINISTA (src/sessions.refresh_account_sessions, SIN LLM), full-scale
-- por cuenta y recomputable.
--
-- Grano SESION: una fila por sesion. session_id = first_conversation_id de la
-- sesion (uuid estable ya existente), llave logica para el drill-down.
--
-- Idempotente (IF NOT EXISTS). El pase la asegura sola al arrancar (self-healing),
-- igual que ensure_indexes / player_conversions; este archivo queda como referencia.

CREATE TABLE IF NOT EXISTS conversation_sessions (
    account       text        NOT NULL,   -- 'sistemas' | 'datos'
    ticket_id     uuid        NOT NULL,    -- ticket al que pertenece la sesion
    session_id    uuid        NOT NULL,    -- = first_conversation_id de la sesion (llave logica)
    sess_no       int         NOT NULL,    -- 0,1,2... dentro del ticket
    start_at      timestamptz,             -- created_at del primer episodio de la sesion
    end_at        timestamptz,             -- created_at del ultimo episodio de la sesion
    episode_count int,                     -- cantidad de episodios de la sesion
    PRIMARY KEY (account, session_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_sessions_ticket ON conversation_sessions (account, ticket_id);
CREATE INDEX IF NOT EXISTS idx_conv_sessions_sid    ON conversation_sessions (session_id);

-- Mapeo episodio -> sesion (grano EPISODIO). Tabla puente separada en vez de una
-- columna en conversation_sessions: esa tabla es grano SESION (PK (account,
-- session_id)); el mapeo es grano conversation. Separarlos mantiene cada grano
-- en su tabla natural.
CREATE TABLE IF NOT EXISTS conversation_session_map (
    conversation_id uuid NOT NULL,         -- episodio (conversations.id)
    account         text NOT NULL,
    session_id      uuid NOT NULL,         -- sesion a la que pertenece el episodio
    PRIMARY KEY (conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_sess_map_sid ON conversation_session_map (account, session_id);
