"""Clasificación LLM de la ATENCIÓN del operador (empujó vs pasivo) para el cuadro
conversión vs pasividad del análisis.

SUPERSEDED por el pase LLM unificado (src/prompts.py); ya no se usa en el worker.
Las reglas de este prompt fueron portadas al pase unificado; este módulo queda como
documentación/tests de esas reglas.

El detector determinista (CTA-keyword) daba una barra demasiado blanda (1-7% pasivo)
vs el 20-50% del análisis, porque cuenta como "activo" cualquier mención de embudo.
El negocio quiere la barra del análisis, que era un JUICIO del modelo. Este es un
pase dedicado (clon liviano de analizar_grande.py): prompt simple, format:json, que
juzga SOLO el comportamiento del OPERADOR humano (no si el jugador depositó — ese es
el otro eje, determinista).
"""
from __future__ import annotations

from src.prompts import format_transcript

# empujo = impulsó activamente el registro/depósito/apuesta.
# pasivo = solo respondió/saludó/informó sin impulsar la conversión.
# no_respondio = prácticamente no atendió lo que el cliente necesitaba.
ATENCION_LABELS = ("empujo", "pasivo", "no_respondio")

PASSIVITY_SCHEMA = {
    "type": "object",
    "properties": {"atencion": {"type": "string", "enum": list(ATENCION_LABELS)}},
    "required": ["atencion"],
}

_SYSTEM = (
    "Sos analista de calidad de una operación de apuestas. Clasificás en UNA etiqueta "
    "el comportamiento del OPERADOR (Agente) frente a un jugador nuevo. NO juzgás si el "
    "jugador terminó depositando (eso es otro eje); solo el ESFUERZO del operador.\n"
    "- empujo: el operador IMPULSÓ CONCRETAMENTE la conversión. Requiere una acción "
    "real: ofrecer/guiar el registro, pedir datos para crear la cuenta, invitar a "
    "depositar/recargar/apostar, mandar un link, o presentar la promo/bono. Si no hay "
    "NINGUNA de esas acciones, NO es empujo.\n"
    "- pasivo: el operador solo saludó, hizo una pregunta suelta, informó o respondió "
    "una duda SIN impulsar la conversión. Un simple 'Hola', 'en qué te ayudo', o una "
    "pregunta trivial = pasivo (no ofreció nada).\n"
    "- no_respondio: prácticamente no atendió lo que el cliente necesitaba.\n"
    "Ejemplos: 'Hola' -> pasivo. 'te ayudo a crear tu cuenta?' -> empujo. 'en qué le "
    "puedo ayudar?' -> pasivo. 'registrate y hacé tu primera recarga de $5' -> empujo.\n"
    'Respondé SOLO JSON: {"atencion":"empujo|pasivo|no_respondio"}.'
)


def build_passivity_prompt(transcript: str) -> tuple[str, str]:
    """(system, user) para clasificar la atención del operador en una conversación."""
    user = ("Conversación (Agente = operador, Cliente = jugador):\n\n"
            f"{transcript}\n\nClasificá la atención del OPERADOR en una etiqueta.")
    return _SYSTEM, user


def classify_passivity(llm, messages: list[dict]) -> str | None:
    """Etiqueta de atención del operador, o None si el LLM no devolvió algo válido
    (el batch deja la fila sin clasificar para reintentar)."""
    transcript = format_transcript(messages, "human")
    system, user = build_passivity_prompt(transcript)
    try:
        out = llm.chat_json(system, user, PASSIVITY_SCHEMA)
    except Exception:  # noqa: BLE001 - un fallo del LLM no debe romper el batch
        return None
    label = str(out.get("atencion", "")).strip().lower()
    return label if label in ATENCION_LABELS else None
