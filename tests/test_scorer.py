"""Tests del orquestador de scoring v2 (score_by_motivo).

El scorer arma el prompt de motivo, llama al LLM (aca falseado) y aplica la
estrella determinista desde la etiqueta. El LLM clasifica el motivo y califica;
nunca decide la estrella.
"""
import pytest

from src.scorer import ScoreResult, score_by_motivo

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


def _motivo_resp(**over):
    resp = {
        "motivo": "deposito",
        "dimensions": {
            "resolucion": "acredito el comprobante",
            "iniciativa": "menciono un bono a alcanzar",
            "cortesia": "saludo por el nombre",
            "errores": [],
        },
        "rating_label": "buena",
        "rating_rationale": "acredito rapido y ofrecio un bono",
        "atencion": "empujo",
        "deposit_observed": True,
    }
    resp.update(over)
    return resp


def test_devuelve_motivo_y_estrella_determinista():
    r = score_by_motivo(target_messages=MSGS, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert isinstance(r, ScoreResult)
    assert r.motivo == "deposito"
    assert r.rating_label == "buena" and r.stars == 4   # determinista, no lo dijo el LLM
    assert r.llm_model == "qwen3.5:4b"
    assert r.dimensions["iniciativa"] == "menciono un bono a alcanzar"


def test_rechaza_motivo_invalido():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="chacharacha")))


def test_rechaza_etiqueta_invalida():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(rating_label="optima")))


def test_rechaza_salida_sin_claves_requeridas():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM({"motivo": "deposito", "rating_label": "buena"}))


def test_pasa_deposit_hint_al_prompt():
    llm = FakeLLM(_motivo_resp())
    score_by_motivo(target_messages=MSGS, thread_context="", llm=llm, deposit_hint=True)
    assert "HINT DETERMINISTA" in llm.calls[0][0]


def test_atencion_ausente_degrada_a_none_sin_descartar_rating():
    resp = _motivo_resp()
    del resp["atencion"]
    r = score_by_motivo(target_messages=MSGS, thread_context="", llm=FakeLLM(resp))
    assert r.rating_label == "buena" and r.atencion is None


def test_atencion_fuera_del_enum_degrada_a_none():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(atencion="mas_o_menos")))
    assert r.atencion is None and r.motivo == "deposito"


def test_deposit_observed_string_false_no_se_invierte():
    # bool('false') es True en Python: deposit_observed debe parsearse, no castearse.
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(deposit_observed="false")))
    assert r.deposit_observed is False


def test_deposit_observed_ausente_es_none():
    resp = _motivo_resp()
    del resp["deposit_observed"]
    r = score_by_motivo(target_messages=MSGS, thread_context="", llm=FakeLLM(resp))
    assert r.deposit_observed is None


def test_deposit_observed_ambiguo_degrada_a_none():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(deposit_observed="no sé")))
    assert r.deposit_observed is None
