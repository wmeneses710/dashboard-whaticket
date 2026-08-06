"""Tests de la Capa 1 de recomendaciones deterministas (src/recommendations.py).

Estos fragmentos son coaching ASPIRACIONAL: nunca afectan la nota, solo
anteponen consejos accionables que el LLM casi nunca produce por si solo.
"""
from src.recommendations import (
    _FRAG_APP,
    _FRAG_PASSWORD,
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


# --- enlace de registro: FRAGMENTO RETIRADO ----------------------------------
# El negocio lo marco como falso el 2026-08-06: "no existe ni codigo de afiliado y
# por gusto mandar el link de sorti si ellos hacen el registro". Afirmaba DOS cosas
# falsas — que hay un codigo de afiliado y que el cliente se registra solo — y era el
# texto mas repetido de todo el coaching: **151 de 624 recomendaciones (24,2%)**, todas
# en `registro`. No lo escribia el modelo: lo anteponia este modulo, asi que ningun
# cambio de prompt lo sacaba.

def test_ya_no_existe_el_fragmento_del_enlace_de_registro():
    import src.recommendations as rec
    assert not hasattr(rec, "_FRAG_REGISTER_LINK")


def test_registro_sin_link_ya_no_inventa_un_codigo_de_afiliado():
    msgs = [_client("quiero registrarme"), _agent("dale, te explico como hacerlo")]
    out = refine_recomendacion("consejo del LLM", motivo="registro", target_messages=msgs)
    assert "afiliado" not in out.lower()
    assert "enlace de registro" not in out.lower()
    assert out == "consejo del LLM"


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
