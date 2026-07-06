"""Rubricas de scoring y mapeo determinista etiqueta -> estrella.

Dos rubricas segun QUIEN atendio la conversacion (no por segmento de negocio):
  - human: la atendio un agente (conversations.user_id presente)
  - bot:   la atendio el chatbot (sin agente)

El scoring es HOLISTICO: el LLM lee la conversacion, llena las dimensiones como
evidencia cualitativa y elige UNA sola etiqueta (rating_label). La estrella es
traduccion DETERMINISTA de esa etiqueta (esta tabla, que controlamos nosotros),
NO una salida del modelo -> los LLM clasifican bien pero calibran mal los numeros.

La dimension `dominant` pone el techo: si falla, la etiqueta no puede superar
"deficiente" (esa regla se instruye en el prompt, ver src/prompts.py).
Ver tambien db/scores_schema.sql.
"""
from __future__ import annotations

from dataclasses import dataclass

Rubric = str  # "human" | "bot"


@dataclass(frozen=True)
class Dimension:
    """Un eje de evaluacion, con ancla de que es 'bien' y que es 'mal'."""

    key: str
    bien: str
    mal: str


@dataclass(frozen=True)
class RubricSpec:
    name: Rubric
    dominant: str                        # dimension que pone el techo
    dimensions: tuple[Dimension, ...]
    labels_desc: tuple[str, ...]         # etiquetas de la mejor (5) a la peor (1)
    label_to_stars: dict[str, int]


HUMAN = RubricSpec(
    name="human",
    dominant="resolucion",
    dimensions=(
        Dimension("empatia",
                  "reconoce la situacion/emocion del cliente, valida, trato humano",
                  "frio, robotico, ignora el reclamo"),
        Dimension("claridad",
                  "explica claro, sin ambiguedad, info correcta y ordenada",
                  "confuso, contradictorio, con jerga"),
        Dimension("resolucion",
                  "atiende el motivo de ESTA visita y lo hace avanzar",
                  "evade, no responde el punto, deja igual al cliente"),
        Dimension("tono",
                  "profesional, cordial y respetuoso",
                  "seco, cortante o agresivo"),
    ),
    labels_desc=("excelente", "buena", "aceptable", "deficiente", "mala"),
    label_to_stars={"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1},
)

BOT = RubricSpec(
    name="bot",
    dominant="cobertura_info",
    dimensions=(
        Dimension("cobertura_info",
                  "da la info que el cliente pide dentro de su alcance",
                  "no responde lo que se pide"),
        Dimension("capacidad_enganche",
                  "entiende la intencion, evita loops y respuestas irrelevantes",
                  "loops, no entiende, responde fuera de tema"),
        Dimension("derivacion",
                  "deriva a un humano en el momento justo cuando excede su alcance",
                  "no deriva cuando debia, o deriva de mas sin intentar"),
    ),
    labels_desc=("optima", "funcional", "mejorable", "deficiente", "falla"),
    label_to_stars={"optima": 5, "funcional": 4, "mejorable": 3, "deficiente": 2, "falla": 1},
)

RUBRICS: dict[Rubric, RubricSpec] = {"human": HUMAN, "bot": BOT}


def get_rubric(rubric: Rubric) -> RubricSpec:
    """Devuelve la especificacion de la rubrica o falla si no existe."""
    try:
        return RUBRICS[rubric]
    except KeyError:
        raise ValueError(f"rubrica desconocida: {rubric!r} (validas: {sorted(RUBRICS)})")


def label_to_stars(rubric: Rubric, label: str) -> int:
    """Traduce una etiqueta cualitativa a su estrella (1..5), de forma determinista.

    Falla si la etiqueta no pertenece a la rubrica (protege contra un LLM que
    devuelva una etiqueta de la otra rubrica o inventada).
    """
    spec = get_rubric(rubric)
    try:
        return spec.label_to_stars[label]
    except KeyError:
        raise ValueError(
            f"etiqueta {label!r} no valida para rubrica {rubric!r} "
            f"(validas: {list(spec.labels_desc)})"
        )
