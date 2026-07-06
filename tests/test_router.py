"""Tests del router de elegibilidad (capa 1: decide rubrica y si se evalua).

La rubrica se decide por QUIEN respondio de verdad (mensajes humano vs bot por
sent_from), NO por conversations.user_id (que suele venir NULL aunque haya
atendido una persona). Casi todo es 'human'; 'bot' solo el ~0,04% puro bot.
"""
from src.router import decide_eligibility, decide_rubric


def test_rubrica_human_si_hubo_operador_humano():
    assert decide_rubric(agent_message_count=3, bot_message_count=2) == "human"


def test_rubrica_bot_solo_si_todo_fue_bot():
    assert decide_rubric(agent_message_count=0, bot_message_count=4) == "bot"


def test_solo_notas_internas_se_saltea():
    assert decide_eligibility(
        real_message_count=0, customer_message_count=0, business_message_count=0
    ) == ("skipped", "internal_notes_only")


def test_sin_respuesta_del_cliente_se_saltea():
    assert decide_eligibility(
        real_message_count=3, customer_message_count=0, business_message_count=3
    ) == ("skipped", "no_customer_reply")


def test_sin_respuesta_del_negocio_se_saltea():
    # solo hablo el cliente (ni humano ni bot respondieron)
    assert decide_eligibility(
        real_message_count=1, customer_message_count=1, business_message_count=0
    ) == ("skipped", "no_agent_reply")


def test_tamano_anomalo_se_saltea():
    assert decide_eligibility(
        real_message_count=999, customer_message_count=50, business_message_count=949
    ) == ("skipped", "anomalous_size")


def test_conversacion_normal_es_evaluable():
    assert decide_eligibility(
        real_message_count=6, customer_message_count=3, business_message_count=3
    ) == ("evaluated", None)
