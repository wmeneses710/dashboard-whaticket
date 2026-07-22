"""Sub-evaluadores ANGOSTOS (2da pasada del LLM), opcionales y aditivos.

El scorer determinista (src/scorer.py) es la base barata y confiable. Estos son el
bisturí sobre lo DIFUSO que el determinismo no alcanza, y corren SOLO cuando se los
habilita (params `verifier`/`recommender` en score_by_motivo):

- verify_uplift: adjudica el BORDERLINE de uplift — buena/excelente que el modelo
  reclamó pero sin señal concreta (¿esfuerzo real o cortesía de plantilla?). UNA sola
  pregunta, prompt adversarial y distinto del scorer (mitiga el punto ciego compartido).
- build_recomendacion: genera el consejo de coaching como TAREA dedicada (no mezclada
  en el prompt de scoring), opcionalmente con ejemplos few-shot.

Cada uno hace UNA cosa: un modelo chico rinde mucho mejor así que juzgando todo junto.
"""
from __future__ import annotations

from typing import Protocol

from src.prompts import format_transcript


class LLM(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict | None = ...) -> dict: ...


# --- Verificador de uplift ------------------------------------------------

_VERIFY_SYSTEM = """\
Sos un auditor ESTRICTO de calidad de atencion. Mira SOLO esta interaccion y decidi UNA cosa:
¿el AGENTE hizo un esfuerzo EXTRA real, mas alla de atender el motivo minimo?

CUENTA como esfuerzo extra: empujar un registro/deposito CONCRETO (mandar link, pedir los
datos, invitar a recargar), ofrecer un bono puntual, retener (invitar a volver a jugar),
o acompañar/confirmar/prevenir el proximo problema.

NO cuenta (esto es el minimo, no extra): saludar con el nombre (aunque sea de plantilla),
jerga afectuosa (bro/amigo/ñaño), emojis, o simplemente EXPLICAR la promo.

Se estricto: si no podes CITAR textualmente el esfuerzo extra, es que no existe.
Responde SOLO con JSON: {"uplift_real": true|false, "evidencia": "<cita textual o 'ninguna'>"}"""


def _verify_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "uplift_real": {"type": "boolean"},
            "evidencia": {"type": "string"},
        },
        "required": ["uplift_real"],
    }


def verify_uplift(target_messages: list[dict], motivo: str, llm: LLM) -> dict:
    """Adjudica si hubo uplift REAL. Devuelve {uplift_real: bool, evidencia: str}.

    Tolerante: si el LLM falla o devuelve algo raro, uplift_real=False (conservador:
    ante la duda NO se licencia buena/excelente; el determinismo ya topo en aceptable).
    """
    user = f"MOTIVO: {motivo}\n\n### CONVERSACION\n{format_transcript(target_messages, motivo)}"
    try:
        raw = llm.chat_json(_VERIFY_SYSTEM, user, _verify_schema())
    except Exception:
        return {"uplift_real": False, "evidencia": ""}
    return {"uplift_real": raw.get("uplift_real") is True, "evidencia": str(raw.get("evidencia") or "")}


# --- Generador de recomendacion (coaching) --------------------------------

_RECOM_SYSTEM = """\
Eres un coach de agentes de atencion al cliente de una plataforma de apuestas. Basandote en
la conversacion, da UN consejo concreto y accionable (1 frase) de como el agente pudo llegar
al SIGUIENTE nivel en este motivo — usa la accion extra esperada del motivo (el UPLIFT).
Debe ser ESPECIFICO a lo que paso, no generico. Si ya fue excelente, devuelve "".

USA ESPANOL NEUTRO Y PROFESIONAL. Imperativo o segunda persona con "tu" ("confirma",
"invita", "envia el enlace"). PROHIBIDO el voseo y los regionalismos: nada de "para",
"mira", "dale", "animate", "bro", "che". Ejemplo neutro: "Invita al cliente a...".
{ejemplos}
Responde SOLO con JSON: {{"recomendacion": "<consejo o cadena vacia>"}}"""

# Ejemplos por motivo (español neutro, concretos, apuntando al UPLIFT). Se usan como
# few-shot por defecto; el lever real contra la genericidad. "may or may not use examples".
_RECOM_EXAMPLES: dict[str, list[str]] = {
    "deposito": ["Confirmaste la recarga; la proxima, menciona el bono que puede alcanzar con su siguiente deposito."],
    "retiro": ["Procesaste el retiro; invita al cliente a volver a jugar o a recargar para retenerlo."],
    "registro": ["Explicaste el registro; envia el enlace directo y guia el primer deposito para cerrar el alta."],
    "soporte_cuenta": ["Resolviste el tramite; confirma que quedo solucionado y anticipa el proximo paso."],
    "info": ["Respondiste la consulta; aprovecha para invitar a un deposito o registro concreto."],
    "promo": ["Explicaste la promocion; envia el enlace de registro e invita a realizar el primer deposito para activarla."],
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
