"""Armado y persistencia de filas en conversation_scores (UPSERT idempotente).

`build_score_record` es logica pura (testeable sin DB): junta datos de la
conversacion + metricas + router + (si aplica) el resultado del LLM en el dict
de columnas. `upsert_score` lo escribe por conversation_id.

La tabla es derivada y separada de las del ETL: es seguro TRUNCARLA y
re-scorear. Ver db/scores_schema.sql.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from src.metrics import (
    MessageStats,
    first_response_seconds,
    resolution_seconds,
    was_unassigned,
)
from src.scorer import ScoreResult
from src.segments import segment_for_queue

SCORING_VERSION = "2026.07-motivo-v2"

# =============================================================================
# Forma CANÓNICA de conversation_scores (grano SESIÓN, todas las columnas
# actuales). store.py es la FUENTE de esta forma; db/scores_schema.sql debe
# mantenerse en sync con estas sentencias.
#
# Sin BEGIN/COMMIT ni ALTER de retrocompat: es la tabla FRESCA que crea la
# migración "desde cero con backup". No lleva `%` para no colisionar con el
# paramstyle de psycopg. Idempotente por CREATE ... IF NOT EXISTS.
# =============================================================================
_CREATE_SCORES_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_scores (
    conversation_id         uuid PRIMARY KEY,
    account                 text NOT NULL,
    ticket_id               uuid,
    segment                 text,
    queue_name              text,
    channel                 text,
    user_id                 uuid,
    user_name               text,
    conversation_created_at timestamptz,
    resolved_at             timestamptz,

    rubric                  text NOT NULL,
    eval_status             text NOT NULL,
    skip_reason             text,

    first_response_seconds  numeric,
    resolution_seconds      numeric,
    message_count           integer,
    agent_message_count     integer,
    bot_message_count       integer,
    contact_message_count   integer,
    was_unassigned          boolean,

    dimensions              jsonb,
    llm_model               text,

    rating_label            text,
    rating_rationale        text,

    resultado               text,
    deposit_count           integer,

    stars                   numeric,
    stars_breakdown         jsonb,

    is_estimate             boolean NOT NULL DEFAULT true,
    scoring_version         text,
    scored_at               timestamptz NOT NULL DEFAULT now(),

    atencion                text,
    deposit_observed        boolean,
    deposit_mismatch        boolean,
    session_id              uuid,
    -- Pase v2: motivo de la interaccion clasificado por el LLM (deposito, retiro,
    -- soporte_cuenta, info, promo, registro, problema). NULL en filas skipped o del
    -- pase viejo. Sin CHECK: la validez la garantiza el enum del schema del scorer.
    motivo                  text,

    -- rating_applicable: LEGACY de la Opción B (adquisición sin rating). v2 la retiró
    -- (promo/registro se califican por su motivo). Queda como true en toda fila
    -- scoreada; se conserva por compatibilidad con queries/dashboard.
    rating_applicable       boolean NOT NULL DEFAULT true,

    CONSTRAINT chk_rubric      CHECK (rubric IN ('human', 'bot')),
    CONSTRAINT chk_eval_status CHECK (eval_status IN ('evaluated', 'skipped')),
    CONSTRAINT chk_eval_coherence CHECK (
        (eval_status = 'skipped'   AND stars IS NULL     AND skip_reason IS NOT NULL) OR
        (eval_status = 'evaluated' AND skip_reason IS NULL)
    ),
    CONSTRAINT chk_stars_range CHECK (stars IS NULL OR (stars >= 1 AND stars <= 5))
)"""

# Índices de db/scores_schema.sql + idx por session_id (grano sesión).
_SCORES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_scores_account_segment ON conversation_scores (account, segment)",
    "CREATE INDEX IF NOT EXISTS idx_scores_user            ON conversation_scores (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_scores_created         ON conversation_scores (conversation_created_at)",
    "CREATE INDEX IF NOT EXISTS idx_scores_rubric_status   ON conversation_scores (rubric, eval_status)",
    "CREATE INDEX IF NOT EXISTS idx_scores_session         ON conversation_scores (session_id)",
)

# Nombre del backup de la tabla previa (grano conversación) que deja la migración.
_SCORES_BACKUP_TABLE = "conversation_scores_pre_session"


def _create_fresh_scores(cur) -> None:
    """Crea la tabla fresca conversation_scores + índices (idempotente)."""
    cur.execute(_CREATE_SCORES_TABLE)
    for stmt in _SCORES_INDEXES:
        cur.execute(stmt)


def ensure_session_scoring_migration(cur) -> dict:
    """Migración AUTOMÁTICA e IDEMPOTENTE "desde cero con backup".

    Al arrancar el servicio: renombra la tabla vieja conversation_scores a un
    backup (`conversation_scores_pre_session`) y crea una tabla FRESCA de grano
    sesión, para empezar el scoring de cero SIN perder lo anterior.

    Idempotente: el gate es la EXISTENCIA del backup.
      - Sin backup + tabla vieja presente -> RENAME + crea fresca. migrated=True.
      - Sin backup + sin tabla vieja (install nueva) -> solo crea fresca. migrated=False
        (no había nada que respaldar, no fue una migración real).
      - Con backup (ya migrado) -> NO re-renombra (no destruye); solo asegura la
        fresca (CREATE IF NOT EXISTS). migrated=False.

    Devuelve {"migrated": bool}; True SOLO cuando efectivamente renombró.
    """
    # Lock de transacción: dos workers arrancando a la vez (rolling deploy) podrían
    # competir en el RENAME. El advisory lock serializa la migración; se libera solo
    # al commit de la transacción del caller. El 2do worker espera y ve el backup ya
    # creado -> no re-renombra.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('conversation_scores_migration'))")
    cur.execute(f"SELECT to_regclass('{_SCORES_BACKUP_TABLE}')")
    backup = cur.fetchone()[0]
    if backup is None:
        cur.execute("SELECT to_regclass('conversation_scores')")
        old = cur.fetchone()[0]
        migrated = old is not None
        if migrated:
            cur.execute(
                f"ALTER TABLE conversation_scores RENAME TO {_SCORES_BACKUP_TABLE}"
            )
            # RENAME TABLE NO renombra los indices: quedan con sus nombres canonicos
            # pegados al backup, y el CREATE INDEX IF NOT EXISTS de la fresca los
            # saltearia (colision de nombre) dejandola SIN indices -> dashboard lento.
            # Liberamos los nombres canonicos renombrando los indices del backup.
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s",
                (_SCORES_BACKUP_TABLE,),
            )
            for (idxname,) in cur.fetchall():
                if not idxname.endswith("_presess"):
                    cur.execute(f'ALTER INDEX "{idxname}" RENAME TO "{idxname}_presess"')
        _create_fresh_scores(cur)
        return {"migrated": migrated}
    # Ya migrado: no tocar el backup ni la fresca existente, solo asegurar forma.
    _create_fresh_scores(cur)
    return {"migrated": False}

_COLUMNS = (
    "conversation_id", "account", "ticket_id", "segment", "queue_name", "channel",
    "user_id", "user_name", "conversation_created_at", "resolved_at",
    "rubric", "eval_status", "skip_reason",
    "first_response_seconds", "resolution_seconds",
    "message_count", "agent_message_count", "bot_message_count",
    "contact_message_count", "was_unassigned",
    "dimensions", "llm_model", "rating_label", "rating_rationale",
    "stars", "stars_breakdown", "deposit_count", "is_estimate", "scoring_version",
    "atencion", "deposit_observed", "deposit_mismatch", "session_id",
    "rating_applicable", "motivo",
)

# Columnas nuevas del pase LLM unificado. ensure_scores_columns() las agrega a una
# tabla de prod ya creada (el CREATE ... IF NOT EXISTS no agrega columnas). Mismo
# patron self-healing que conversions.ensure_table.
_SCORES_COLUMN_TYPES = (
    ("atencion", "text"),
    ("deposit_observed", "boolean"),
    ("deposit_mismatch", "boolean"),
    ("session_id", "uuid"),
    ("rating_applicable", "boolean NOT NULL DEFAULT true"),
    ("motivo", "text"),
)


def ensure_scores_columns(cur) -> None:
    """Agrega las columnas del pase LLM unificado si faltan (idempotente)."""
    for col, coltype in _SCORES_COLUMN_TYPES:
        cur.execute(
            f"ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS {col} {coltype}"
        )


def build_score_record(
    *,
    conversation: dict,
    stats: MessageStats,
    rubric: str,
    eval_status: str,
    skip_reason: str | None,
    score: ScoreResult | None,
    operator_id=None,
    operator_name: str | None = None,
    deposit_count: int = 0,
    session_id=None,
    scoring_version: str = SCORING_VERSION,
) -> dict[str, Any]:
    """Arma el dict de columnas para conversation_scores.

    `operator_id`/`operator_name` = operador reconstruido desde los mensajes (el
    conversations.user_id suele venir NULL). was_unassigned refleja el flag de
    asignacion de whaticket (conversations.user_id).
    """
    c = conversation
    segment = segment_for_queue(c.get("queue_name"))
    record: dict[str, Any] = {
        "conversation_id": c["id"],
        "account": c.get("account"),
        "ticket_id": c.get("ticket_id"),
        "segment": segment,
        "queue_name": c.get("queue_name"),
        "channel": c.get("channel"),
        "user_id": operator_id,
        "user_name": operator_name,
        "conversation_created_at": c.get("created_at"),
        "resolved_at": c.get("resolved_at"),
        "rubric": rubric,
        "eval_status": eval_status,
        "skip_reason": skip_reason,
        "first_response_seconds": first_response_seconds(
            c["created_at"], c.get("first_sent_message_at")
        ),
        "resolution_seconds": resolution_seconds(c["created_at"], c.get("resolved_at")),
        "message_count": stats.message_count,
        "agent_message_count": stats.agent_message_count,
        "bot_message_count": stats.bot_message_count,
        "contact_message_count": stats.contact_message_count,
        "was_unassigned": was_unassigned(c.get("user_id")),
        "dimensions": None,
        "llm_model": None,
        "rating_label": None,
        "rating_rationale": None,
        "stars": None,
        "stars_breakdown": None,
        "deposit_count": deposit_count,
        "is_estimate": True,
        "scoring_version": scoring_version,
        # Pase LLM unificado. En el path por-conversacion session_id llega None (lo
        # llena el paso 2). atencion/deposit_observed solo si hubo score.
        "atencion": None,
        "deposit_observed": None,
        "deposit_mismatch": _deposit_mismatch(deposit_count, score),
        "session_id": session_id,
        # Motivo v2: lo llena el score (score_by_motivo). None en skipped / pase viejo.
        "motivo": None,
        # v2: el rating (por MOTIVO) aplica SIEMPRE que haya evaluación. Se retiró la
        # supresión Opción B en adquisición: promo/registro tienen su propia rúbrica y
        # SÍ se califican. Columna conservada (siempre true en filas scoreadas) por
        # compatibilidad con queries/dashboard.
        "rating_applicable": True,
    }
    if score is not None:
        record.update(
            llm_model=score.llm_model,
            atencion=score.atencion,
            deposit_observed=score.deposit_observed,
            motivo=score.motivo,
            dimensions=score.dimensions,
            rating_label=score.rating_label,
            rating_rationale=score.rating_rationale,
            stars=score.stars,
            stars_breakdown={
                "rubric": score.rubric,
                "label": score.rating_label,
                "stars": score.stars,
                "scoring_version": scoring_version,
            },
        )
    return record


def _deposit_mismatch(deposit_count: int, score: ScoreResult | None) -> bool | None:
    """Reconciliacion determinista vs LLM del deposito (senal de calidad de dato).

    None si no se puede reconciliar (sin score o el LLM no observo el deposito).
    Si no: True cuando el gate determinista (deposit_count>0) y la observacion del
    LLM discrepan. El determinista manda; el flag solo marca la discrepancia.
    """
    if score is None or score.deposit_observed is None:
        return None
    return (deposit_count > 0) != score.deposit_observed


# Columnas JSONB que hay que envolver para psycopg.
_JSONB_COLS = {"dimensions", "stars_breakdown"}


def upsert_score(cur, record: dict) -> None:
    """Inserta o actualiza la fila por conversation_id (idempotente)."""
    cols = list(_COLUMNS)
    placeholders = ", ".join(f"%({col})s" for col in cols)
    updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in cols if col != "conversation_id")
    sql = (
        f"INSERT INTO conversation_scores ({', '.join(cols)}, scored_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT (conversation_id) DO UPDATE SET {updates}, scored_at = now()"
    )
    params = {
        col: (Jsonb(record[col]) if col in _JSONB_COLS and record[col] is not None else record[col])
        for col in cols
    }
    cur.execute(sql, params)
