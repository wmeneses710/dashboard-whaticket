"""Tests del orquestador de scoring v2 (score_by_motivo).

El LLM emite HECHOS concretos (atendio/extra/cortesia/maltrato) y el CODIGO deriva
la etiqueta (label_from_facts) y la estrella. Ademas hay overrides deterministas de
los hechos (senal dura le gana al modelo) y el guard de motivo por comprobante.
"""
import pytest

from src.scorer import ScoreResult, score_by_motivo

# Mensajes NEUTROS: no disparan ninguna senal determinista (ni confirmacion, ni
# media, ni push, ni maltrato) -> sirven para testear la derivacion PURA.
NEUTRAL = [
    {"from_me": False, "is_note": False, "body": "una consulta"},
    {"from_me": True, "is_note": False, "body": "buenas, decime"},
]
# Deposito con confirmacion del agente (dispara agent_resolved).
MSGS = [
    {"from_me": False, "is_note": False, "body": "no me llego la recarga"},
    {"from_me": True, "is_note": False, "body": "ya te la acredito"},
]
# Con EMPUJE concreto del agente (link) -> agent_pushed=True. Necesario para que
# buena/excelente sobrevivan el cap de uplift (PIEZA 2).
PUSH = [
    {"from_me": False, "is_note": False, "body": "quiero el bono"},
    {"from_me": True, "is_note": False, "body": "Registrate acá https://www.sorti.ec/register y aprovechá"},
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
    """Salida base del LLM: hechos que derivan a 'aceptable' (atendio, sin extra)."""
    resp = {
        "motivo": "info",
        "dimensions": {
            "resolucion": "respondio la consulta",
            "iniciativa": "no ofrecio nada extra",
            "cortesia": "cordial",
            "errores": [],
        },
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "rating_rationale": "respondio correctamente",
        "recomendacion": "podrias invitar a un deposito",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# --- derivacion HECHOS -> etiqueta ----------------------------------------

def test_atendio_solo_es_aceptable():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert isinstance(r, ScoreResult)
    assert r.motivo == "info"
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.llm_model == "qwen3.5:4b"


def test_no_atendio_es_deficiente():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(atendio_el_motivo=False)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_atendio_mas_extra_con_empuje_es_buena():
    # buena requiere empuje concreto (PIEZA 2): con el link sobrevive el cap.
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True)))
    assert r.rating_label == "buena" and r.stars == 4


def test_atendio_extra_y_cortesia_con_empuje_es_excelente():
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True, cortesia_destacada=True)))
    assert r.rating_label == "excelente" and r.stars == 5


# --- PIEZA 2: cap de uplift (buena/excelente sin empuje -> aceptable) ------

def test_cap_uplift_buena_sin_empuje_baja_a_aceptable():
    # el LLM dice extra+cortesia (deriva buena/excelente) pero NO hay empuje concreto
    # (solo plantilla/jerga) -> se topa en aceptable.
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(hizo_accion_extra=True, cortesia_destacada=True)))
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.floor_applied is True


# --- PIEZA 1: piso del front-of-funnel (flujo de anuncio) -----------------

def test_piso_funnel_info_con_empuje_no_es_deficiente():
    # el LLM dice que NO atendió, pero el agente mandó link/promo (piso del flujo anuncio)
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info", atendio_el_motivo=False,
                                                 hizo_accion_extra=False, cortesia_destacada=False)))
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.floor_applied is True


def test_problema_no_se_floorea_por_empuje():
    # en 'problema' un empuje comercial NO es resolución -> sigue deficiente si el LLM dijo que no atendió
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema", atendio_el_motivo=False,
                                                 hizo_accion_extra=False, cortesia_destacada=False)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_deposit_observed_string_false_no_se_invierte():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(deposit_observed="false")))
    assert r.deposit_observed is False


def test_recomendacion_pasa_al_resultado():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert r.recomendacion == "podrias invitar a un deposito"


# --- validacion de salida -------------------------------------------------

def test_rechaza_motivo_invalido():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="chacharacha")))


def test_rechaza_salida_sin_hechos_requeridos():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM({"motivo": "info", "rating_rationale": "x"}))


def test_atencion_ausente_degrada_a_none():
    resp = _motivo_resp()
    del resp["atencion"]
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(resp))
    assert r.atencion is None


def test_atencion_fuera_del_enum_degrada_a_none():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(atencion="mas_o_menos")))
    assert r.atencion is None


# --- guard de motivo por comprobante --------------------------------------

def test_deposit_hint_corrige_retiro_a_deposito():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="retiro")), deposit_hint=True)
    assert r.motivo == "deposito"


def test_deposit_hint_no_toca_otros_motivos():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info")), deposit_hint=True)
    assert r.motivo == "info"


def test_deposit_hint_corrige_problema_a_deposito_con_confirmacion():
    msgs = [{"from_me": False, "is_note": False, "body": "Abono 10 a deuda"},
            {"from_me": True, "is_note": False, "body": "ing"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema")), deposit_hint=True)
    assert r.motivo == "deposito"


def test_deposit_hint_no_toca_problema_sin_confirmacion():
    msgs = [{"from_me": False, "is_note": False, "body": "mandé comprobante y no me acreditan"},
            {"from_me": True, "is_note": False, "body": "déjame revisar con el área"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema")), deposit_hint=True)
    assert r.motivo == "problema"


# --- overrides deterministas de HECHOS ------------------------------------

def test_override_atendio_si_agente_confirmo_en_transaccional():
    # el LLM dice que NO atendio, pero el agente confirmo ("acredito") en un deposito
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito", atendio_el_motivo=False)))
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.floor_applied is True


def test_mala_sin_maltrato_detectado_no_cae_a_mala():
    # el LLM marca maltrato pero no hay insulto real -> se descarta -> no es 'mala'
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", atendio_el_motivo=False,
                                                 hubo_maltrato_grave=True)))
    assert r.rating_label == "deficiente" and r.floor_applied is True


def test_mala_con_maltrato_detectado_se_respeta():
    msgs = [{"from_me": False, "is_note": False, "body": "ayuda"},
            {"from_me": True, "is_note": False, "body": "no seas tonto, ya te dije"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema", hubo_maltrato_grave=True)))
    assert r.rating_label == "mala" and r.stars == 1


# --- atencion #5 ----------------------------------------------------------

def test_atencion_empujo_si_agente_manda_link():
    msgs = [{"from_me": False, "is_note": False, "body": "cómo me registro"},
            {"from_me": True, "is_note": False, "body": "Regístrate acá https://www.sorti.ec/register"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro", atencion="pasivo")))
    assert r.atencion == "empujo"


def test_pasa_deposit_hint_al_prompt():
    llm = FakeLLM(_motivo_resp())
    score_by_motivo(target_messages=MSGS, thread_context="", llm=llm, deposit_hint=True)
    assert "HINT DETERMINISTA" in llm.calls[0][0]
