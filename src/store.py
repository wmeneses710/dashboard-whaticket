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

SCORING_VERSION = "2026.07-v1"

_COLUMNS = (
    "conversation_id", "account", "ticket_id", "segment", "queue_name", "channel",
    "user_id", "user_name", "conversation_created_at", "resolved_at",
    "rubric", "eval_status", "skip_reason",
    "first_response_seconds", "resolution_seconds",
    "message_count", "agent_message_count", "bot_message_count",
    "contact_message_count", "was_unassigned",
    "dimensions", "llm_model", "rating_label", "rating_rationale",
    "stars", "stars_breakdown", "is_estimate", "scoring_version",
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
    scoring_version: str = SCORING_VERSION,
) -> dict[str, Any]:
    """Arma el dict de columnas para conversation_scores.

    `operator_id`/`operator_name` = operador reconstruido desde los mensajes (el
    conversations.user_id suele venir NULL). was_unassigned refleja el flag de
    asignacion de whaticket (conversations.user_id).
    """
    c = conversation
    record: dict[str, Any] = {
        "conversation_id": c["id"],
        "account": c.get("account"),
        "ticket_id": c.get("ticket_id"),
        "segment": segment_for_queue(c.get("queue_name")),
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
        "is_estimate": True,
        "scoring_version": scoring_version,
    }
    if score is not None:
        record.update(
            dimensions=score.dimensions,
            llm_model=score.llm_model,
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
