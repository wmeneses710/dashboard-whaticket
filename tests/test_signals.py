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
    agent_strong_uplift,
    client_abandoned,
    client_asked_question,
    client_reasked,
)

# La plantilla real del flujo de anuncio (pitch-only): NO es empuje concreto.
_AD_TEMPLATE = (
    "Debes registrarte, verificar tu cuenta y con tu primer deposito activas todas las "
    "promociones. No te pierdas la promo, aprovechala. Anímate y me avisas.")


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


def test_media_type_no_real_no_cuenta():
    # 'chat'/'missed'/'template' NO son media real (un texto guardado como 'chat' no cuenta)
    assert agent_sent_media([_agent(media_type="chat")]) is False
    assert agent_sent_media([_agent(media_type="missed")]) is False
    assert agent_sent_media([_agent(media_type="document")]) is True  # doc sí es media real


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


def test_push_ofrecer_registro_o_promo():
    assert agent_pushed([_agent("¿te creo un usuario?")]) is True
    assert agent_pushed([_agent("tenemos un bono del 100% para vos")]) is True
    assert agent_pushed([_agent("te ayudo a registrarte")]) is True


# --- client_asked_question ------------------------------------------------

def test_client_asked_question_true():
    assert client_asked_question([_client("¿cómo reclamo los 10 giros?")]) is True
    assert client_asked_question([_client("quiero saber cuánto es el mínimo")]) is True


def test_client_asked_question_false_solo_saludo():
    assert client_asked_question([_client("hola"), _client("gracias"), _client("ok")]) is False


# --- agent_maltrato (unico gatillo de 'mala') -----------------------------

def test_maltrato_insulto_explicito():
    assert agent_maltrato([_agent("no seas tonto, ya te expliqué")]) is True


def test_jerga_amistosa_no_es_maltrato():
    # "panita"/"ñaño"/"pana" son trato afectuoso ecuatoriano, NO maltrato
    assert agent_maltrato([_agent("listo mi pana, cualquier cosa avisas ñaño")]) is False


def test_saludo_normal_no_es_maltrato():
    assert agent_maltrato([_agent("Hola, gracias por comunicarte 🙂")]) is False


# --- agent_strong_uplift (empuje CONCRETO para licenciar buena/excelente) --

def test_strong_uplift_link():
    assert agent_strong_uplift([_agent("Registrate acá https://www.sorti.ec/register")]) is True


def test_strong_uplift_imperativo():
    assert agent_strong_uplift([_agent("te invito a depositar y jugar")]) is True
    assert agent_strong_uplift([_agent("depositá ya y activás el bono")]) is True


def test_strong_uplift_pide_datos():
    assert agent_strong_uplift([_agent("pasame tu nombre y cédula para crearte la cuenta")]) is True


def test_plantilla_de_anuncio_NO_es_uplift_concreto():
    # el caso real: 'con tu primer deposito activas...' + 'aprovecha' + 'Anímate' es PISO,
    # no empuje concreto -> no debe licenciar buena/excelente (evita el 5★ de la plantilla).
    assert agent_strong_uplift([_agent(_AD_TEMPLATE)]) is False
    # pero SÍ dispara el push amplio (piso del funnel):
    assert agent_pushed([_agent(_AD_TEMPLATE)]) is True


# --- client_reasked (fricción determinista) -------------------------------

def test_reasked_corrida_larga_sin_respuesta():
    # el cliente manda 5 seguidos sin ninguna respuesta del negocio -> fricción
    msgs = [_client("hola"), _client("necesito ayuda"), _client("me sale error"),
            _client("hola?"), _client("?")]
    assert client_reasked(msgs) is True


def test_reasked_pings_de_desesperacion_en_corrida():
    # corrida corta pero con pings claros ("ayuda", "?") sin respuesta -> fricción
    msgs = [_client("hice un deposito"), _client("ayuda"), _client("?")]
    assert client_reasked(msgs) is True


def test_reasked_multitransaccion_no_es_friccion():
    # cliente manda mucho PERO el agente responde entre medio (Abono->ing) -> NO fricción
    msgs = [_client("Abono 5"), _agent("ing"), _client("Abono 10"), _agent("ing"),
            _client("Abono 15"), _agent("ing"), _client("Abono 20"), _agent("ing")]
    assert client_reasked(msgs) is False


def test_reasked_intercambio_normal_no_es_friccion():
    msgs = [_client("cuanto es el minimo?"), _agent("$5"), _client("gracias")]
    assert client_reasked(msgs) is False


def test_reasked_respuesta_del_bot_corta_la_corrida():
    # si el bot responde, el cliente no quedó sin respuesta -> no es ghosteo
    msgs = [_client("hola"), _bot("¡Hola! ¿En qué te ayudo?"), _client("info"),
            _client("por favor")]
    assert client_reasked(msgs) is False


def test_reasked_vacio_o_sin_cliente():
    assert client_reasked([]) is False
    assert client_reasked([_agent("hola")]) is False
