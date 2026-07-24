"""Tests de la Capa 1 de recomendaciones deterministas (src/recommendations.py).

Estos fragmentos son coaching ASPIRACIONAL: nunca afectan la nota, solo
anteponen consejos accionables que el LLM casi nunca produce por si solo.
"""
from src.recommendations import (
    _FRAG_APP,
    _FRAG_PASSWORD,
    _FRAG_REGISTER_LINK,
    refine_recomendacion,
)


def _agent(body=""):
    return {"from_me": True, "is_note": False, "body": body}


def _client(body=""):
    return {"from_me": False, "is_note": False, "body": body}


# --- credenciales ----------------------------------------------------------

def test_credenciales_antepone_fragmento_de_password():
    msgs = [_agent("tu usuario es juan123 tu contraseña es abc456")]
    out = refine_recomendacion("consejo del LLM", motivo="soporte_cuenta", target_messages=msgs)
    assert out.startswith(_FRAG_PASSWORD)
    assert out.endswith("consejo del LLM")


# --- app mencionada ----------------------------------------------------------

def test_app_mencionada_incluye_fragmento():
    msgs = [_client("¿tienen app?")]
    out = refine_recomendacion("consejo del LLM", motivo="info", target_messages=msgs)
    assert _FRAG_APP in out
    assert out.endswith("consejo del LLM")


# --- enlace de registro ------------------------------------------------------

def test_registro_sin_link_ni_credenciales_pide_enviar_link():
    msgs = [_client("quiero registrarme"), _agent("dale, te explico como hacerlo")]
    out = refine_recomendacion("consejo del LLM", motivo="registro", target_messages=msgs)
    assert _FRAG_REGISTER_LINK in out


def test_registro_con_link_no_pide_enviarlo():
    msgs = [_client("quiero registrarme"),
            _agent("Regístrate acá https://www.sorti.ec/register?code=1")]
    out = refine_recomendacion("consejo del LLM", motivo="registro", target_messages=msgs)
    assert _FRAG_REGISTER_LINK not in out


def test_registro_con_credenciales_no_pide_enviar_link():
    # si ya se dieron credenciales (alta manual), no tiene sentido pedir el link de registro
    msgs = [_agent("tu usuario es juan123 tu contraseña es abc456")]
    out = refine_recomendacion("consejo del LLM", motivo="registro", target_messages=msgs)
    assert _FRAG_REGISTER_LINK not in out


def test_motivo_distinto_de_registro_no_dispara_fragmento_de_link():
    msgs = [_client("quiero depositar"), _agent("dale, decime cuanto")]
    out = refine_recomendacion("consejo del LLM", motivo="deposito", target_messages=msgs)
    assert _FRAG_REGISTER_LINK not in out


# --- sin señales -------------------------------------------------------------

def test_sin_senales_devuelve_la_recomendacion_del_llm_intacta():
    msgs = [_client("hola"), _agent("buenas, decime")]
    out = refine_recomendacion("consejo del LLM", motivo="info", target_messages=msgs)
    assert out == "consejo del LLM"


def test_recomendacion_vacia_con_senal_no_deja_espacios_colgantes():
    msgs = [_agent("tu usuario es juan123 tu contraseña es abc456")]
    out = refine_recomendacion("", motivo="soporte_cuenta", target_messages=msgs)
    assert out == _FRAG_PASSWORD
    assert not out.endswith(" ")
    assert "  " not in out


# --- orden y combinacion -----------------------------------------------------

def test_fragmentos_lideran_sobre_la_reco_del_llm():
    msgs = [_agent("tu usuario es juan123 tu contraseña es abc456. descarga la app")]
    out = refine_recomendacion("consejo del LLM", motivo="soporte_cuenta", target_messages=msgs)
    assert out.index(_FRAG_PASSWORD) < out.index(_FRAG_APP) < out.index("consejo del LLM")
