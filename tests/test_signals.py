"""Tests de las senales deterministas de resolucion (src/signals.py).

Son la capa que corrige la dureza sistematica del LLM: detecta -sin modelo- que
el agente SI atendio el motivo (confirmo una transaccion o mando el comprobante),
para que el scorer no lo hunda por debajo del piso. Mensajes = dicts con
from_me, is_note, body, media_type, sent_from.
"""
from src.signals import (
    agent_confirmation,
    agent_maltrato,
    agent_pushed,
    agent_resolved,
    agent_sent_media,
    client_abandoned,
)


def _agent(body="", media_type=None):
    return {"from_me": True, "is_note": False, "body": body, "media_type": media_type}


def _client(body="", media_type=None):
    return {"from_me": False, "is_note": False, "body": body, "media_type": media_type}


def _bot(body=""):
    return {"from_me": True, "is_note": False, "body": body, "sent_from": "CHATBOT"}


# --- agent_confirmation ---------------------------------------------------

def test_confirmacion_token_corto_ing():
    assert agent_confirmation([_client("no me llego"), _agent("ing")]) is True


def test_confirmacion_saldo_disponible():
    assert agent_confirmation([_agent("Tu saldo ya está disponible. Suerte 🍀")]) is True


def test_confirmacion_en_breve_retiro():
    assert agent_confirmation([_agent("Tu retiro está en proceso, en breve el comprobante")]) is True


def test_confirmacion_cargado_mayus():
    assert agent_confirmation([_agent("CARGADO")]) is True


def test_sin_confirmacion_solo_saludo():
    assert agent_confirmation([_agent("Hola, ¿en qué te ayudo?")]) is False


def test_confirmacion_ignora_al_cliente():
    # el CLIENTE diciendo "listo" no es confirmacion del agente
    assert agent_confirmation([_client("listo, ya te mando")]) is False


def test_confirmacion_ignora_al_bot():
    assert agent_confirmation([_bot("Tu saldo ya está disponible")]) is False


# --- agent_sent_media (comprobante/tutorial del agente) -------------------

def test_agente_mando_imagen_es_media():
    assert agent_sent_media([_client("quiero retirar"), _agent(media_type="image")]) is True


def test_agente_mando_video_tutorial():
    assert agent_sent_media([_agent("mira este tutorial", media_type="video")]) is True


def test_media_del_cliente_no_cuenta():
    assert agent_sent_media([_client(media_type="image")]) is False


# --- client_abandoned -----------------------------------------------------

def test_cliente_abandono_si_ultimo_es_agente():
    msgs = [_client("hola"), _agent("¿en qué te ayudo?")]
    assert client_abandoned(msgs) is True


def test_no_abandono_si_cliente_respondio_ultimo():
    msgs = [_agent("¿en qué te ayudo?"), _client("gracias")]
    assert client_abandoned(msgs) is False


def test_abandono_ignora_notas_finales():
    msgs = [_client("hola"), _agent("listo"), {"from_me": True, "is_note": True, "body": "*resuelto*"}]
    assert client_abandoned(msgs) is True


# --- agent_resolved (confirmacion o media del agente) ---------------------

def test_agent_resolved_por_confirmacion():
    assert agent_resolved([_agent("ingresado")]) is True


def test_agent_resolved_por_media():
    assert agent_resolved([_agent(media_type="image")]) is True


def test_agent_no_resolved_solo_saludo():
    assert agent_resolved([_agent("buenas, ¿en qué ayudo?")]) is False


# --- agent_pushed (empuje comercial) --------------------------------------

def test_push_por_link():
    assert agent_pushed([_agent("Regístrate acá https://www.sorti.ec/register?code=1")]) is True


def test_push_por_invitacion_bono_recarga():
    assert agent_pushed([_agent("Recuerda que por tu segunda recarga obtienes un bono del 150%")]) is True


def test_push_te_invito():
    assert agent_pushed([_agent("Te invito a entrar en el siguiente link")]) is True


def test_no_push_solo_informa():
    assert agent_pushed([_agent("El horario de atención es de 9 a 18")]) is False


# --- agent_maltrato (unico gatillo de 'mala') -----------------------------

def test_maltrato_insulto_explicito():
    assert agent_maltrato([_agent("no seas tonto, ya te expliqué")]) is True


def test_jerga_amistosa_no_es_maltrato():
    # "panita"/"ñaño"/"pana" son trato afectuoso ecuatoriano, NO maltrato
    assert agent_maltrato([_agent("listo mi pana, cualquier cosa avisas ñaño")]) is False


def test_saludo_normal_no_es_maltrato():
    assert agent_maltrato([_agent("Hola, gracias por comunicarte 🙂")]) is False
