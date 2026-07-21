"""Tests del orquestador de scoring de UNA conversacion.

El scorer arma el prompt, llama al LLM (aca falseado) y aplica la estrella
determinista desde la etiqueta. El LLM nunca decide la estrella.
"""
import pytest

from src.scorer import ScoreResult, score_by_motivo, score_conversation

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


def _unified_resp(**over):
    """Salida del pase LLM UNIFICADO (rating + atencion + observacion de deposito)."""
    resp = {
        "dimensions": {
            "resolucion": "acredito la recarga",
            "empatia": "correcto",
            "claridad": "claro",
            "tono": "cordial",
            "errores": [],
        },
        "rating_label": "buena",
        "rating_rationale": "resolvio rapido el reclamo de recarga",
        "atencion": "empujo",
        "deposit_observed": True,
    }
    resp.update(over)
    return resp


def test_score_aplica_estrella_determinista():
    llm = FakeLLM(_unified_resp())

    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)

    assert isinstance(r, ScoreResult)
    assert r.rating_label == "buena"
    assert r.stars == 4                       # deterministico, no lo dijo el LLM
    assert r.llm_model == "qwen3.5:4b"
    assert r.dimensions["resolucion"] == "acredito la recarga"
    assert len(llm.calls) == 1


def test_score_propaga_atencion_y_deposit_observed():
    # PIEZA 3 — pase unificado: de UNA lectura salen rating + atencion + deposito.
    llm = FakeLLM(_unified_resp(atencion="pasivo", deposit_observed=False))

    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)

    assert r.rating_label == "buena"
    assert r.stars == 4                       # la estrella sigue determinista
    assert r.atencion == "pasivo"
    assert r.deposit_observed is False


def test_score_rechaza_etiqueta_invalida_para_la_rubrica():
    # dims completas (validas) pero etiqueta 'optima' es de bot, no de human.
    llm = FakeLLM(_unified_resp(rating_label="optima"))
    with pytest.raises(ValueError):
        score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)


def test_score_rechaza_salida_sin_claves_requeridas():
    llm = FakeLLM({"rating_label": "buena"})  # faltan dimensions y rationale
    with pytest.raises(ValueError):
        score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=llm)


def test_atencion_ausente_degrada_a_none_sin_descartar_el_rating():
    # Un atencion faltante NO debe descartar un rating por lo demas valido (si no, la
    # conversacion quedaria atascada reintentando LLM para siempre).
    resp = _unified_resp()
    del resp["atencion"]
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=FakeLLM(resp))
    assert r.rating_label == "buena" and r.atencion is None


def test_atencion_fuera_del_enum_degrada_a_none():
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="",
                           llm=FakeLLM(_unified_resp(atencion="mas_o_menos")))
    assert r.atencion is None and r.rating_label == "buena"


def test_deposit_observed_string_false_no_se_invierte():
    # bool('false') es True en Python: deposit_observed debe parsearse, no castearse.
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="",
                           llm=FakeLLM(_unified_resp(deposit_observed="false")))
    assert r.deposit_observed is False


def test_deposit_observed_ausente_es_none():
    resp = _unified_resp()
    del resp["deposit_observed"]
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="", llm=FakeLLM(resp))
    assert r.deposit_observed is None


def test_deposit_observed_ambiguo_degrada_a_none():
    # Un valor inconcluso ("no sé") NO debe leerse como False (dispararia un
    # deposit_mismatch falso); degrada a None como atencion fuera del enum.
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="",
                           llm=FakeLLM(_unified_resp(deposit_observed="no sé")))
    assert r.deposit_observed is None


def test_deposit_observed_string_true_es_true():
    r = score_conversation(rubric="human", target_messages=MSGS, thread_context="",
                           llm=FakeLLM(_unified_resp(deposit_observed="true")))
    assert r.deposit_observed is True


# --- pase v2: score_by_motivo (el LLM clasifica el motivo) --------------------
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


def test_score_by_motivo_devuelve_motivo_y_estrella_determinista():
    r = score_by_motivo(target_messages=MSGS, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert r.motivo == "deposito"
    assert r.rating_label == "buena" and r.stars == 4
    assert r.dimensions["iniciativa"] == "menciono un bono a alcanzar"


def test_score_by_motivo_rechaza_motivo_invalido():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="chacharacha")))


def test_score_by_motivo_pasa_deposit_hint_al_prompt():
    llm = FakeLLM(_motivo_resp())
    score_by_motivo(target_messages=MSGS, thread_context="", llm=llm, deposit_hint=True)
    assert "HINT DETERMINISTA" in llm.calls[0][0]


def test_score_by_motivo_atencion_fuera_de_enum_degrada_a_none():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(atencion="x")))
    assert r.atencion is None and r.motivo == "deposito"
