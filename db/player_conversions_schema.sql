-- Tabla de conversión de jugadores potenciales -> jugadores (nivel PERSONA).
--
-- Una fila por persona NUEVA (is_new_contact) del segmento jugador. Precomputada
-- por un pase DETERMINISTA (src/conversions.py, SIN LLM): recorre las tablas del
-- ETL y deja, por persona, su primera interacción (operador, canal, fecha) y si
-- depositó. El dashboard la AGREGA (por operador, por mes) en milisegundos y la
-- filtra, sin re-escanear los 2M mensajes.
--
-- Grano PERSONA a propósito: la conversión es first-touch (una persona convierte
-- una vez o no). Se guardan contact_id + first_conversation_id como LLAVES para
-- el drill-down: de "operador X floja en abril" -> sus personas -> sus mensajes.
--
-- Idempotente (IF NOT EXISTS). El pase la asegura sola al arrancar (self-healing),
-- igual que ensure_indexes; este archivo queda como referencia del esquema.

CREATE TABLE IF NOT EXISTS player_conversions (
    account               text        NOT NULL,   -- 'sistemas' | 'datos'
    contact_id            text        NOT NULL,    -- persona (tickets.contact_id, como texto)
    first_at              timestamptz,             -- 1ª interacción = entrada (cohorte por mes)
    first_conversation_id uuid,                    -- llave para abrir la conversación de entrada
    user_id               uuid,                    -- operador de la 1ª interacción (NULL = bot/sin asignar)
    channel               text,
    segment               text,                    -- 'jugador' (por ahora único)
    deposited             boolean     NOT NULL DEFAULT false,  -- depósito determinista (comprobante+recarga)
    returned              boolean     NOT NULL DEFAULT false,  -- re-engagement: VOLVIÓ (>= 2 sesiones)
    return_session_id     uuid,                    -- 2da sesión cronológica (llave del regreso)
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, contact_id)
);

-- Agregación por operador y por mes de entrada (los dos ejes del cuadro).
CREATE INDEX IF NOT EXISTS idx_player_conv_op    ON player_conversions (account, user_id);
CREATE INDEX IF NOT EXISTS idx_player_conv_month ON player_conversions (account, first_at);
