"""Sub-evaluadores ANGOSTOS (2da pasada del LLM), opcionales y aditivos.

El scorer determinista (src/scorer.py) es la base barata y confiable. Estos son el
bisturí sobre lo DIFUSO que el determinismo no alcanza, y corren SOLO cuando se los
habilita (param `recommender` en score_by_motivo):

- build_recomendacion: genera el consejo de coaching como TAREA dedicada (no mezclada
  en el prompt de scoring), opcionalmente con ejemplos few-shot.

Hace UNA cosa: un modelo chico rinde mucho mejor asi que juzgando todo junto.

HUBO UN SEGUNDO sub-evaluador, `verify_uplift`, que adjudicaba el borderline del cap de
uplift de `promo` (la PIEZA 2 del scorer). Se retiro junto con ese cap el 2026-08-11: el
cap era inalcanzable -`promo` es 100% determinista, asi que score_by_motivo retorna antes-
y al medir el criterio contra dos desenlaces independientes resulto que apuntaba al REVES
(las sesiones con material y SIN empuje fuerte convierten 24,8% contra 5,7% de las que
tienen los dos). Sin cap no habia borderline que rescatar.
"""
from __future__ import annotations

from typing import Protocol

from src.prompts import format_transcript


class LLM(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict | None = ...) -> dict: ...


_RECOM_SYSTEM = """\
Eres un coach de operadores de atencion al cliente de una plataforma de apuestas. Basandote en
la conversacion, da UN consejo concreto y accionable (1 frase) de como el operador pudo llegar
al SIGUIENTE nivel en este motivo — usa la accion extra esperada del motivo (el UPLIFT).
Debe ser ESPECIFICO a lo que paso, no generico. Si ya fue excelente, devuelve "".

USA ESPANOL NEUTRO Y PROFESIONAL. Imperativo o segunda persona con "tu" ("confirma",
"invita", "pide los datos"). PROHIBIDO el voseo y los regionalismos: nada de "para",
"mira", "dale", "animate", "bro", "che". Ejemplo neutro: "Invita al cliente a...".
NUNCA hables de un "enlace de registro" ni de un "flyer": en este negocio el registro lo
hace el OPERADOR pidiendo los datos, no existe un link que mandar, y el equipo no reconoce
esos artefactos. Si el consejo es mandar algo, decilo por lo que es: una imagen o un video.
{ejemplos}
Responde SOLO con JSON: {{"recomendacion": "<consejo o cadena vacia>"}}"""

# Ejemplos por motivo (español neutro, concretos, apuntando al UPLIFT). Se usan como
# few-shot por defecto; el lever real contra la genericidad. "may or may not use examples".
_RECOM_EXAMPLES: dict[str, list[str]] = {
    "deposito": ["Confirmaste la recarga; la proxima, menciona el bono que puede alcanzar con su siguiente deposito."],
    "retiro": ["Procesaste el retiro; invita al cliente a volver a jugar o a recargar para retenerlo."],
    "registro": ["Explicaste el registro; pide los datos y crea la cuenta para cerrar el alta."],
    "soporte_cuenta": ["Resolviste el tramite; confirma que quedo solucionado y anticipa el proximo paso."],
    "info": ["Respondiste la consulta; aprovecha para invitar a un deposito o registro concreto."],
    "promo": ["Explicaste la promocion; mandale una imagen de la promo e invitalo a recargar para activarla."],
    "problema": ["Atendiste el reclamo; haz seguimiento y confirma la solucion para prevenir que se repita."],
}


def _recom_schema() -> dict:
    return {
        "type": "object",
        "properties": {"recomendacion": {"type": "string"}},
        "required": ["recomendacion"],
    }


def build_recomendacion(
    target_messages: list[dict], motivo: str, label: str, llm: LLM,
    examples: list[str] | None = None,
) -> str:
    """Genera el consejo de coaching como tarea dedicada. `examples` (opcional) = lista de
    consejos ejemplares para few-shot; si es None se usan los del motivo (_RECOM_EXAMPLES).
    Devuelve "" si falla o si ya fue excelente."""
    if label == "excelente":
        return ""
    if examples is None:
        examples = _RECOM_EXAMPLES.get(motivo)
    ejemplos = ""
    if examples:
        ejemplos = "\nEjemplos de buenos consejos (neutros):\n" + "\n".join(f"- {e}" for e in examples) + "\n"
    system = _RECOM_SYSTEM.format(ejemplos=ejemplos)
    user = (f"MOTIVO: {motivo}\nNOTA OBTENIDA: {label}\n\n### CONVERSACION\n"
            f"{format_transcript(target_messages, motivo)}")
    try:
        raw = llm.chat_json(system, user, _recom_schema())
    except Exception:
        return ""
    return str(raw.get("recomendacion") or "")
