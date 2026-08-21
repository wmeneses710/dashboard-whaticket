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


def decide_rubric(*, operator_message_count: int, bot_message_count: int) -> str:
    """Rubrica segun QUIEN respondio de verdad (por sent_from), no por asignacion.

    'bot' solo si TODO el negocio fue bot (el ~0,04% puro bot); en cuanto hubo un
    operador humano es 'human'. Los mixtos (bot saluda + humano atiende) son
    'human': la calidad la puso la persona.
    """
    if operator_message_count > 0:
        return "human"
    if bot_message_count > 0:
        return "bot"
    return "human"  # sin negocio: se saltea igual por no_agent_reply


def decide_eligibility(
    *,
    real_message_count: int,
    customer_message_count: int,
    business_message_count: int,
    customer_text_count: int | None = None,
    operator_resolved: bool = False,
) -> tuple[str, str | None]:
    """Devuelve (eval_status, skip_reason).

    `business_message_count` = mensajes del negocio (humano + bot, from_me).
    `customer_text_count` = mensajes del cliente con TEXTO legible (opcional por
    compatibilidad). `operator_resolved` = senal determinista de que el operador atendio
    (confirmo la transaccion o mando el comprobante; ver src/signals.py). Orden: sin
    contenido real -> sin cliente -> cliente solo media (y operador NO resolvio) ->
    sin respuesta del negocio -> tamaño anomalo -> evaluable.

    Sin respuesta del negocio no hay accion que evaluar (p. ej. una visita con
    solo un "Gracias" del cliente). Si el cliente SOLO mando imagenes/audio
    (customer_text_count == 0) el LLM no puede leer su intencion... SALVO que el
    operador haya resuelto: en el flujo estandar de deposito el cliente manda solo el
    comprobante y el operador confirma ("saldo disponible"), asi que el motivo es
    inferible del operador y NO se debe saltear (la auditoria mostro que este skip
    tiraba a la basura el motivo de mayor volumen).
    """
    if real_message_count == 0:
        return "skipped", "internal_notes_only"
    if customer_message_count == 0:
        return "skipped", "no_customer_reply"
    if customer_text_count is not None and customer_text_count == 0 and not operator_resolved:
        return "skipped", "customer_media_only"
    # `no_agent_reply` YA NO SE SALTEA (decision del negocio, 2026-08-21). Eran 1.167
    # sesiones, y el skip escondia la peor falla que este sistema puede medir: el cliente
    # escribio y nadie contesto. Peor: medido sobre 300 de ellas, el **99,7% tiene una nota
    # del CRM "<Nombre> *resuelto* la conversacion"** -- un operador la cerro sin escribirle
    # nunca al cliente. Eso es deliberado y atribuible, no un descuido.
    # La nota la pone `src/sin_respuesta.score_sin_respuesta` (1 estrella, determinista), y
    # la razon viaja en `dimensions.sin_respuesta_del_negocio` porque el CHECK de la tabla
    # borra `skip_reason` en las filas evaluadas.
    # El chequeo del contador SE QUEDA aca aunque ya no skipee: es la unica señal que este
    # gate tiene, y el que decide es el worker con los mensajes en la mano.
    if real_message_count > ANOMALOUS_MESSAGE_MAX:
        return "skipped", "anomalous_size"
    return "evaluated", None
