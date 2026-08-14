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
    """True si la conversacion nunca tuvo un operador asignado (la atendio el bot)."""
    return user_id is None


def _is_bot(message: dict) -> bool:
    """True si el mensaje lo genero el chatbot (sent_from=CHATBOT)."""
    return message.get("sent_from") == BOT_SENT_FROM


@dataclass(frozen=True)
class MessageStats:
    message_count: int          # mensajes reales (sin notas)
    operator_message_count: int    # negocio humano (from_me, no bot), sin notas
    bot_message_count: int      # negocio bot (sent_from=CHATBOT), sin notas
    contact_message_count: int  # cliente (from_me=False), sin notas
    # cliente con TEXTO legible (body no vacio). Si es 0 pero contact_message_count>0,
    # el cliente solo mando media -> el LLM no puede leerlo (ver router).
    contact_text_message_count: int


def message_stats(messages: list[dict]) -> MessageStats:
    """Cuenta mensajes reales separando cliente / humano / bot (por sent_from)."""
    real = [m for m in messages if not m.get("is_note")]
    business = [m for m in real if m.get("from_me")]
    contact = [m for m in real if not m.get("from_me")]
    bot = sum(1 for m in business if _is_bot(m))
    agent = len(business) - bot
    contact_text = sum(1 for m in contact if (m.get("body") or "").strip())
    return MessageStats(
        message_count=len(real),
        operator_message_count=agent,
        bot_message_count=bot,
        contact_message_count=len(contact),
        contact_text_message_count=contact_text,
    )


def reparto_por_interaccion(messages: list[dict]) -> tuple[int, int]:
    """(cuantas interacciones tuvo la sesion, cuantos operadores DISTINTOS las atendieron).

    EXISTE PARA QUE LA FILA NO MIENTA EN SILENCIO. El tablero valida la interaccion
    OPERADOR->CLIENTE y cada interaccion se le asigna a alguien, pero una fila de
    `conversation_scores` es UNA nota con UN operador: si la sesion tuvo varias visitas con
    gente distinta, la nota se la lleva el de mas mensajes y el trabajo del resto desaparece.

    MEDIDO el 2026-08-14 sobre v15 (15.562 sesiones evaluadas):
        83,2% (12.948) una sola interaccion         -> atribucion honesta
        16,8% ( 2.614) multi-interaccion
                2.110  ...con UN SOLO operador      -> atribucion honesta igual
                  504  ...con VARIOS operadores     -> 3,2%, el caso a marcar
    En esas 504 hay 2.734 interacciones y **1.824 (66,7%) son de un operador que NO recibio
    la nota**; llegan a 10 operadores en una sola fila.

    NO SE MUEVE LA VENTANA para arreglarlo: cualquier ventana deja el 66,7% afuera del que
    cobra. Partir la sesion es la solucion de raiz y el negocio la rechazo con numeros (ver
    docs/handoff.md §10). Se MARCA, que es el mismo patron de `interaccion_juzgada_desde`.

    Una interaccion sin operador identificable no suma: dejar 'sin identificar' como si
    fuera una persona mas seria inventar un operador.
    """
    from src.interacciones import partir_en_interacciones

    if not messages:
        return (0, 0)
    partes = partir_en_interacciones(messages)
    duenos = {str(d) for d in (primary_operator(p) for p in partes) if d is not None}
    return (len(partes), len(duenos))


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
