"""Tests de las metricas objetivas deterministas (sin LLM, sin DB)."""
from datetime import datetime, timedelta, timezone

from src.metrics import (
    first_response_seconds,
    message_stats,
    primary_operator,
    resolution_seconds,
    was_unassigned,
)

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_first_response_seconds():
    assert first_response_seconds(T0, T0 + timedelta(seconds=45)) == 45


def test_first_response_none_si_no_hubo_respuesta():
    assert first_response_seconds(T0, None) is None


def test_resolution_seconds():
    assert resolution_seconds(T0, T0 + timedelta(minutes=5)) == 300


def test_resolution_none_si_no_resuelta():
    assert resolution_seconds(T0, None) is None


def test_message_stats_separa_humano_bot_y_cliente():
    msgs = [
        {"from_me": False, "is_note": False, "body": "hola"},
        {"from_me": True, "is_note": True, "body": "nota interna"},
        {"from_me": True, "is_note": False, "body": "menu", "sent_from": "CHATBOT"},
        {"from_me": True, "is_note": False, "body": "te ayudo", "sent_from": "WEB"},
        {"from_me": False, "is_note": False, "body": "gracias"},
    ]
    s = message_stats(msgs)
    assert s.message_count == 4           # excluye la nota
    assert s.contact_message_count == 2   # cliente
    assert s.agent_message_count == 1     # WEB = humano
    assert s.bot_message_count == 1       # CHATBOT = bot real


def test_message_stats_sin_sent_from_cuenta_como_humano():
    # sin sent_from asumimos operador humano (no bot)
    msgs = [{"from_me": True, "is_note": False, "body": "hola"}]
    s = message_stats(msgs)
    assert s.agent_message_count == 1
    assert s.bot_message_count == 0


def test_primary_operator_toma_el_mas_frecuente():
    msgs = [
        {"from_me": True, "is_note": False, "sent_from": "WEB", "user_id": "op-A"},
        {"from_me": True, "is_note": False, "sent_from": "WEB", "user_id": "op-A"},
        {"from_me": True, "is_note": False, "sent_from": "WEB", "user_id": "op-B"},
        {"from_me": True, "is_note": False, "sent_from": "CHATBOT", "user_id": None},
    ]
    assert primary_operator(msgs) == "op-A"


def test_primary_operator_none_si_solo_bot():
    msgs = [{"from_me": True, "is_note": False, "sent_from": "CHATBOT", "user_id": None}]
    assert primary_operator(msgs) is None


def test_was_unassigned():
    assert was_unassigned(None) is True
    assert was_unassigned("52010cb3") is False
