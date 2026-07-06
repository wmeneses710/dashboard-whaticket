"""Metricas objetivas deterministas por conversacion (capa 2, sin LLM).

Todo lo que se calcula con SQL/aritmetica y no necesita el modelo: tiempos y
conteos. Los conteos EXCLUYEN las notas internas (is_note), igual que el
transcript que ve el LLM.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

# Marca de mensaje generado por el chatbot (resto = operador humano).
BOT_SENT_FROM = "CHATBOT"


def first_response_seconds(created_at: datetime, first_sent_at: datetime | None) -> float | None:
    """Segundos desde que se creo la conversacion hasta el primer mensaje enviado."""
    if first_sent_at is None:
        return None
    return (first_sent_at - created_at).total_seconds()


def resolution_seconds(created_at: datetime, resolved_at: datetime | None) -> float | None:
    """Segundos desde la creacion hasta la resolucion."""
    if resolved_at is None:
        return None
    return (resolved_at - created_at).total_seconds()


def was_unassigned(user_id) -> bool:
    """True si la conversacion nunca tuvo un agente asignado (la atendio el bot)."""
    return user_id is None


def _is_bot(message: dict) -> bool:
    """True si el mensaje lo genero el chatbot (sent_from=CHATBOT)."""
    return message.get("sent_from") == BOT_SENT_FROM


@dataclass(frozen=True)
class MessageStats:
    message_count: int          # mensajes reales (sin notas)
    agent_message_count: int    # negocio humano (from_me, no bot), sin notas
    bot_message_count: int      # negocio bot (sent_from=CHATBOT), sin notas
    contact_message_count: int  # cliente (from_me=False), sin notas


def message_stats(messages: list[dict]) -> MessageStats:
    """Cuenta mensajes reales separando cliente / humano / bot (por sent_from)."""
    real = [m for m in messages if not m.get("is_note")]
    business = [m for m in real if m.get("from_me")]
    bot = sum(1 for m in business if _is_bot(m))
    agent = len(business) - bot
    return MessageStats(
        message_count=len(real),
        agent_message_count=agent,
        bot_message_count=bot,
        contact_message_count=len(real) - len(business),
    )


def primary_operator(messages: list[dict]):
    """user_id del operador HUMANO que mas mensajes envio (None si solo bot).

    Reconstruye el 'quien atendio' desde messages.user_id, porque
    conversations.user_id suele venir NULL aunque haya atendido una persona.
    """
    ids = [
        m.get("user_id")
        for m in messages
        if m.get("from_me") and not m.get("is_note") and not _is_bot(m) and m.get("user_id")
    ]
    if not ids:
        return None
    return Counter(ids).most_common(1)[0][0]
