"""Tests de los sub-evaluadores angostos (2da pasada del LLM), src/subeval.py.

build_recomendacion genera el coaching como tarea dedicada
como tarea dedicada (± ejemplos). El LLM va falseado.
"""
from src.subeval import build_recomendacion

MSGS = [
    {"from_me": False, "is_note": False, "body": "quiero el bono"},
    {"from_me": True, "is_note": False, "body": "te ayudo, registrate y con tu primera recarga lo tenés"},
]


class FakeLLM:
    model = "qwen3:14b"

    def __init__(self, resp, raises=False):
        self.resp = resp
        self.raises = raises
        self.calls = []

    def chat_json(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        if self.raises:
            raise RuntimeError("boom")
        return self.resp


# --- build_recomendacion --------------------------------------------------

def test_recomendacion_devuelve_string():
    r = build_recomendacion(MSGS, "promo", "aceptable", FakeLLM({"recomendacion": "invitá al bono de la 2da recarga"}))
    assert r == "invitá al bono de la 2da recarga"


def test_recomendacion_excelente_es_vacia_sin_llamar():
    llm = FakeLLM({"recomendacion": "no debería usarse"})
    r = build_recomendacion(MSGS, "promo", "excelente", llm)
    assert r == "" and llm.calls == []   # no gastó una llamada


def test_recomendacion_con_ejemplos_los_incluye():
    llm = FakeLLM({"recomendacion": "ok"})
    build_recomendacion(MSGS, "registro", "deficiente", llm, examples=["cerrá el alta y encaminá el 1er depósito"])
    system, user, schema = llm.calls[0]
    assert "Ejemplos" in system and "cerrá el alta" in system


def test_recomendacion_error_del_llm_es_vacia():
    r = build_recomendacion(MSGS, "promo", "aceptable", FakeLLM(None, raises=True))
    assert r == ""


def test_recomendacion_instruye_espanol_neutro():
    llm = FakeLLM({"recomendacion": "ok"})
    build_recomendacion(MSGS, "retiro", "aceptable", llm)
    system = llm.calls[0][0].lower()
    assert "neutro" in system and "voseo" in system


def test_recomendacion_usa_ejemplos_del_motivo_por_defecto():
    # sin pasar examples, toma los del motivo (few-shot por defecto)
    llm = FakeLLM({"recomendacion": "ok"})
    build_recomendacion(MSGS, "retiro", "aceptable", llm)
    system = llm.calls[0][0]
    assert "volver a jugar" in system   # el ejemplo neutro de retiro
