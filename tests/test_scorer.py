"""Tests del orquestador de scoring de UNA conversacion.

El scorer arma el prompt, llama al LLM (aca falseado) y aplica la estrella
determinista desde la etiqueta. El LLM nunca decide la estrella.
"""
import pytest

from src.scorer import ScoreResult, score_conversation

MSGS = [
    {"from_me": False, "is_note": False, "body": "no me llego la recarga"},
    {"from_me": True, "is_note": False, "body": "ya te la acredito"},
]


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def chat_json(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        return self.resp


def test_score_aplica_estrella_determinista():
    llm = FakeLLM({
        "dimensions": {
            "resolucion": "acredito la recarga",
            "empatia": "correcto",
            "claridad": "claro",
            "tono": "cordial",
            "errores": [],
        },
        "rating_label": "buena",
        "rating_rationale": "resolvio rapido el reclamo de recarga",
    })

    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)

    assert isinstance(r, ScoreResult)
    assert r.rating_label == "buena"
    assert r.stars == 4                       # deterministico, no lo dijo el LLM
    assert r.llm_model == "qwen3.5:4b"
    assert r.dimensions["resolucion"] == "acredito la recarga"
    assert len(llm.calls) == 1


def test_score_rechaza_etiqueta_invalida_para_la_rubrica():
    # dims completas (validas) pero etiqueta 'optima' es de bot, no de human.
    llm = FakeLLM({
        "dimensions": {"empatia": "a", "claridad": "b", "resolucion": "c", "tono": "d",
                       "errores": []},
        "rating_label": "optima",
        "rating_rationale": "x",
    })
    with pytest.raises(ValueError):
        score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)


def test_score_rechaza_salida_sin_claves_requeridas():
    llm = FakeLLM({"rating_label": "buena"})  # faltan dimensions y rationale
    with pytest.raises(ValueError):
        score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)
