"""Pase DETERMINISTA de conversión de jugadores (SIN LLM).

Llena player_conversions: una fila por persona NUEVA (is_new_contact) del segmento
jugador, con su primera interacción (operador, canal, fecha), la conversación de
entrada (llave para drill-down) y si depositó (comprobante+recarga determinista).

Corre en segundo plano (worker) y hace UPSERT: `deposited` puede pasar de false->true
cuando la persona deposita más tarde; el resto (entrada) es estable. El dashboard
AGREGA esta tabla (por operador / por mes) en vez de re-escanear los 2M mensajes.

Reutiliza los patrones que ya funcionan en prod: conv_op (operador dominante,
src/queries._LOAD_SQL) y conv_dep (flag de depósito, src/queries._DEP_PCT_SQL).
"""
from __future__ import annotations

from src.deposits import RECHARGE_PATTERN
from src.queries import _jugador_queue_ids

# Idempotente + self-healing (como ensure_indexes): el pase la asegura al correr.
_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS player_conversions (
        account               text        NOT NULL,
        contact_id            text        NOT NULL,
        first_at              timestamptz,
        first_conversation_id uuid,
        user_id               uuid,
        channel               text,
        segment               text,
        deposited             boolean     NOT NULL DEFAULT false,
        attention             text,
        updated_at            timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (account, contact_id)
    )""",
    # attention = atención del operador (empujo|pasivo|no_respondio) tomada del SCORE
    # DE SESIÓN (conversation_scores.atencion) de la conversación de ENTRADA; NULL = la
    # sesión aún no fue scoreada. ALTER para tablas ya creadas por una versión previa.
    "ALTER TABLE player_conversions ADD COLUMN IF NOT EXISTS attention text",
    "CREATE INDEX IF NOT EXISTS idx_player_conv_op    ON player_conversions (account, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_player_conv_month ON player_conversions (account, first_at)",
)

# Recompute full-scale por cuenta -> upsert. Personas nuevas del segmento jugador:
#  - first_conv: su conversación de ENTRADA (la más antigua) -> operador/canal/fecha.
#  - first_op:  operador dominante de esa conversación (más mensajes de negocio humano).
#  - person_deposit: si CUALQUIER conversación de la persona tiene comprobante+recarga.
#  - attention: atención del operador tomada del SCORE DE SESIÓN (conversation_scores
#    .atencion) de la conversación de ENTRADA. Determinista, sin LLM: el pase de sesión
#    ya la clasificó; acá solo se copia.
# contact_id se castea a text (evita mismatch de tipos uuid/bigint; ver bug card_key).
_REFRESH_SQL = """
INSERT INTO player_conversions
      (account, contact_id, first_at, first_conversation_id, user_id, channel, segment, deposited, attention)
WITH jugador_convs AS (
  SELECT c.id AS conv_id, c.created_at, c.is_new_contact,
         t.contact_id::text AS contact_id, t.channel
    FROM conversations c
    JOIN tickets t ON t.id = c.ticket_id
   WHERE c.account = %(account)s AND c.queue_id = ANY(%(qids)s)
     AND c.created_at IS NOT NULL AND t.contact_id IS NOT NULL
),
new_persons AS (
  SELECT DISTINCT contact_id FROM jugador_convs WHERE is_new_contact
),
first_conv AS (
  SELECT DISTINCT ON (jc.contact_id) jc.contact_id, jc.conv_id AS first_conversation_id,
         jc.created_at AS first_at, jc.channel
    FROM jugador_convs jc
    JOIN new_persons np ON np.contact_id = jc.contact_id
   ORDER BY jc.contact_id, jc.created_at ASC
),
msg_op AS (
  SELECT conversation_id, user_id, count(*) AS n
    FROM messages
   WHERE account = %(account)s AND from_me AND NOT is_note AND user_id IS NOT NULL
   GROUP BY conversation_id, user_id
),
first_op AS (
  SELECT DISTINCT ON (conversation_id) conversation_id, user_id
    FROM msg_op ORDER BY conversation_id, n DESC
),
conv_dep AS (
  SELECT conversation_id,
         bool_or((body ~* %(re)s) AND NOT is_note) AS has_ctx,
         count(*) FILTER (WHERE from_me = false AND NOT is_note
                          AND lower(coalesce(media_type, '')) LIKE '%%image%%') AS img
    FROM messages WHERE account = %(account)s GROUP BY conversation_id
),
person_deposit AS (
  SELECT jc.contact_id, bool_or(cd.has_ctx AND cd.img > 0) AS deposited
    FROM jugador_convs jc
    JOIN new_persons np ON np.contact_id = jc.contact_id
    LEFT JOIN conv_dep cd ON cd.conversation_id = jc.conv_id
   GROUP BY jc.contact_id
)
SELECT %(account)s, fc.contact_id, fc.first_at, fc.first_conversation_id,
       fo.user_id, fc.channel, 'jugador', coalesce(pd.deposited, false), cs.atencion
  FROM first_conv fc
  LEFT JOIN first_op fo ON fo.conversation_id = fc.first_conversation_id
  LEFT JOIN person_deposit pd ON pd.contact_id = fc.contact_id
  LEFT JOIN conversation_scores cs ON cs.conversation_id = fc.first_conversation_id
ON CONFLICT (account, contact_id) DO UPDATE
   SET deposited = EXCLUDED.deposited, user_id = EXCLUDED.user_id,
       channel = EXCLUDED.channel, first_at = EXCLUDED.first_at,
       first_conversation_id = EXCLUDED.first_conversation_id,
       -- COALESCE: no pisar un attention bueno con NULL cuando la sesion de entrada
       -- todavia no tiene score (post-migracion la tabla fresca esta vacia; sin este
       -- guard el 1er refresh borraria en masa la columna de pasividad del dashboard).
       attention = COALESCE(EXCLUDED.attention, player_conversions.attention),
       updated_at = now()
"""


def ensure_table(cur) -> None:
    """Crea player_conversions + índices si faltan (idempotente)."""
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)


def refresh_account_conversions(cur, account: str) -> int:
    """Recomputa la conversión de UNA cuenta (determinista, sin LLM) y hace upsert.
    Devuelve la cantidad de filas afectadas. Si la cuenta no tiene colas jugador, 0."""
    ensure_table(cur)
    qids = _jugador_queue_ids(cur, account)
    if not qids:
        return 0
    cur.execute(_REFRESH_SQL, {"account": account, "qids": qids, "re": RECHARGE_PATTERN})
    return cur.rowcount
