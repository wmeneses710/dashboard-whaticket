"""Tests del armado del registro para conversation_scores (parte pura, sin DB)."""
from datetime import datetime, timedelta, timezone

from src.metrics import message_stats
from src.scorer import ScoreResult
from src.store import build_score_record

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

CONV = {
    "id": "conv-1",
    "account": "sistemas",
    "ticket_id": "ticket-1",
    "queue_name": "Jugadores",
    "channel": "WHATSAPP",
    "user_id": "agente-1",
    "created_at": T0,
    "first_sent_message_at": T0 + timedelta(seconds=30),
    "resolved_at": T0 + timedelta(minutes=4),
}

MSGS = [
    {"from_me": False, "is_note": False, "body": "hola"},
    {"from_me": True, "is_note": False, "body": "te ayudo"},
]


def test_registro_evaluado_lleva_estrella_y_segmento():
    score = ScoreResult(
        rubric="human",
        dimensions={"resolucion": "ok", "errores": []},
        rating_label="buena",
        rating_rationale="resolvio bien",
        stars=4,
        llm_model="qwen3.5:4b",
    )
    r = build_score_record(
        conversation=CONV, stats=message_stats(MSGS), rubric="human",
        eval_status="evaluated", skip_reason=None, score=score,
        operator_id="op-123", operator_name="Annel Flores",
        scoring_version="2026.07-v1",
    )
    assert r["segment"] == "jugador"          # via segments.segment_for_queue
    assert r["user_name"] == "Annel Flores"
    assert r["rubric"] == "human"
    assert r["eval_status"] == "evaluated"
    assert r["skip_reason"] is None
    assert r["stars"] == 4
    assert r["rating_label"] == "buena"
    assert r["message_count"] == 2
    assert r["bot_message_count"] == 0
    assert r["user_id"] == "op-123"           # operador reconstruido, no conversations.user_id
    assert r["first_response_seconds"] == 30
    assert r["resolution_seconds"] == 240
    assert r["was_unassigned"] is False       # conversations.user_id no era null
    assert r["stars_breakdown"]["label"] == "buena"
    assert r["is_estimate"] is True


def test_registro_salteado_no_lleva_estrella():
    r = build_score_record(
        conversation={**CONV, "user_id": None}, stats=message_stats(MSGS),
        rubric="bot", eval_status="skipped", skip_reason="no_customer_reply",
        score=None, operator_id=None, scoring_version="2026.07-v1",
    )
    assert r["rubric"] == "bot"
    assert r["eval_status"] == "skipped"
    assert r["skip_reason"] == "no_customer_reply"
    assert r["stars"] is None
    assert r["rating_label"] is None
    assert r["rating_rationale"] is None
    assert r["dimensions"] is None
    assert r["was_unassigned"] is True        # conversations.user_id era null
