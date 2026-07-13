"""Tests del clasificador de pasividad (prompt + parseo). El LLM se mockea."""
from src.passivity import ATENCION_LABELS, build_passivity_prompt, classify_passivity


class _LLM:
    def __init__(self, out=None, boom=False):
        self._out = out
        self._boom = boom

    def chat_json(self, system, user, schema=None):
        if self._boom:
            raise RuntimeError("modelo caído")
        return self._out


MSGS = [{"from_me": False, "body": "hola quiero info", "is_note": False},
        {"from_me": True, "body": "te ayudo a crear tu cuenta", "is_note": False}]


def test_build_passivity_prompt_pide_una_etiqueta_de_operador():
    system, user = build_passivity_prompt("Cliente: hola\nAgente: dale")
    assert "empujo" in system and "pasivo" in system
    assert "OPERADOR" in user and "Cliente: hola" in user


def test_classify_normaliza_etiqueta_valida():
    assert classify_passivity(_LLM({"atencion": "PASIVO"}), MSGS) == "pasivo"
    assert classify_passivity(_LLM({"atencion": "empujo"}), MSGS) == "empujo"


def test_classify_invalida_o_error_da_none():
    assert classify_passivity(_LLM({"atencion": "cualquiera"}), MSGS) is None
    assert classify_passivity(_LLM({}), MSGS) is None
    assert classify_passivity(_LLM(boom=True), MSGS) is None


def test_labels_esperadas():
    assert ATENCION_LABELS == ("empujo", "pasivo", "no_respondio")
