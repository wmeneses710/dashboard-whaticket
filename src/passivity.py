"""Clasificación LLM de la ATENCIÓN del operador (empujó vs pasivo) para el cuadro
conversión vs pasividad del análisis.

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
    "jugador terminó depositando (eso es otro eje); solo si el operador EMPUJÓ hacia la "
    "conversión o fue pasivo.\n"
    "- empujo: impulsó activamente (pidió datos para registrar, mandó link, invitó a "
    "depositar/recargar/apostar, insistió con la promo, guió el alta paso a paso).\n"
    "- pasivo: solo respondió/saludó/informó sin impulsar (contestó la duda y no avanzó, "
    "saludó y esperó, cerró sin empujar).\n"
    "- no_respondio: prácticamente no atendió lo que el cliente necesitaba.\n"
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
