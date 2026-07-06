"""Router de elegibilidad: decide la rubrica y si la conversacion se evalua.

Es la capa 1 (SQL/logica pura, barata) que corre ANTES del LLM. Toda
conversacion termina como una fila en conversation_scores; las no evaluables
llevan eval_status='skipped' + skip_reason para que el dashboard explique la
cobertura sin dañar la estadistica. Ver db/scores_schema.sql.
"""
from __future__ import annotations

# Tope de mensajes reales por conversacion: por encima es patologico (p. ej. un
# loop de bot) y se saltea para no envenenar el contexto del LLM. El truncado
# normal vive en src/prompts.py; esto es el guardarrail duro.
ANOMALOUS_MESSAGE_MAX = 250


def decide_rubric(*, agent_message_count: int, bot_message_count: int) -> str:
    """Rubrica segun QUIEN respondio de verdad (por sent_from), no por asignacion.

    'bot' solo si TODO el negocio fue bot (el ~0,04% puro bot); en cuanto hubo un
    operador humano es 'human'. Los mixtos (bot saluda + humano atiende) son
    'human': la calidad la puso la persona.
    """
    if agent_message_count > 0:
        return "human"
    if bot_message_count > 0:
        return "bot"
    return "human"  # sin negocio: se saltea igual por no_agent_reply


def decide_eligibility(
    *,
    real_message_count: int,
    customer_message_count: int,
    business_message_count: int,
) -> tuple[str, str | None]:
    """Devuelve (eval_status, skip_reason).

    `business_message_count` = mensajes del negocio (humano + bot, from_me).
    Orden: sin contenido real -> sin cliente -> sin respuesta del negocio ->
    tamaño anomalo -> evaluable. Sin respuesta del negocio no hay accion que
    evaluar (p. ej. una visita con solo un "Gracias" del cliente).
    """
    if real_message_count == 0:
        return "skipped", "internal_notes_only"
    if customer_message_count == 0:
        return "skipped", "no_customer_reply"
    if business_message_count == 0:
        return "skipped", "no_agent_reply"
    if real_message_count > ANOMALOUS_MESSAGE_MAX:
        return "skipped", "anomalous_size"
    return "evaluated", None
