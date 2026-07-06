"""Orquesta el scoring SEMANTICO de UNA conversacion.

Arma el prompt (con contexto del hilo), llama al LLM para obtener la
calificacion cualitativa, valida las claves y aplica la estrella determinista
desde la etiqueta. El LLM nunca decide la estrella. La elegibilidad (rubrica,
evaluated/skipped) la decide antes el router; aca ya llega una conversacion
'evaluated'.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.prompts import build_output_schema, build_scorer_prompt
from src.rubrics import label_to_stars


class LLM(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict | None = ...) -> dict: ...


@dataclass(frozen=True)
class ScoreResult:
    rubric: str
    dimensions: dict
    rating_label: str
    rating_rationale: str
    stars: int
    llm_model: str


def _validate(raw: dict, schema: dict) -> None:
    """Verifica que la salida del LLM tenga las claves requeridas.

    Reemplaza la garantia que daria el schema-grammar (que no usamos): pedimos la
    forma en el prompt y la validamos aca.
    """
    for key in schema["required"]:
        if key not in raw:
            raise ValueError(f"salida del LLM sin la clave requerida: {key!r}")
    dims = raw.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("salida del LLM: 'dimensions' debe ser un objeto")
    for key in schema["properties"]["dimensions"]["required"]:
        if key not in dims:
            raise ValueError(f"salida del LLM: falta la dimension {key!r}")


def score_conversation(
    *,
    rubric: str,
    target_messages: list[dict],
    thread_context: str,
    llm: LLM,
) -> ScoreResult:
    """Puntua una conversacion y devuelve el resultado con la estrella aplicada."""
    system, user = build_scorer_prompt(rubric, target_messages, thread_context)
    schema = build_output_schema(rubric)
    raw = llm.chat_json(system, user, schema)  # schema habilita el fallback grammar
    _validate(raw, schema)

    label = raw["rating_label"]
    stars = label_to_stars(rubric, label)  # valida la etiqueta contra la rubrica
    return ScoreResult(
        rubric=rubric,
        dimensions=raw["dimensions"],
        rating_label=label,
        rating_rationale=raw["rating_rationale"],
        stars=stars,
        llm_model=llm.model,
    )
