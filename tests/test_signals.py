"""Tests de las senales deterministas de resolucion (src/signals.py).

Son la capa que corrige la dureza sistematica del LLM: detecta -sin modelo- que
el agente SI atendio el motivo (confirmo una transaccion o mando el comprobante),
para que el scorer no lo hunda por debajo del piso. Mensajes = dicts con
from_me, is_note, body, media_type, sent_from.
"""
from src.signals import (
    operator_acreditacion,
    operator_asked_anything_else,
    operator_acuse,
    operator_confirmation,
    operator_maltrato,
    operator_pushed,
    operator_resolved,
    operator_sent_credentials,
    operator_sent_media,
    operator_sent_register_link,
    operator_strong_uplift,
    app_mentioned,
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


# --- operator_confirmation ---------------------------------------------------

def test_confirmacion_token_corto_ing():
    assert operator_confirmation([_client("no me llego"), _agent("ing")]) is True


def test_confirmacion_saldo_disponible():
    assert operator_confirmation([_agent("Tu saldo ya está disponible. Suerte 🍀")]) is True


def test_confirmacion_en_breve_retiro():
    assert operator_confirmation([_agent("Tu retiro está en proceso, en breve el comprobante")]) is True


def test_confirmacion_cargado_mayus():
    assert operator_confirmation([_agent("CARGADO")]) is True


def test_sin_confirmacion_solo_saludo():
    assert operator_confirmation([_agent("Hola, ¿en qué te ayudo?")]) is False


def test_confirmacion_ignora_al_cliente():
    # el CLIENTE diciendo "listo" no es confirmacion del agente
    assert operator_confirmation([_client("listo, ya te mando")]) is False


def test_confirmacion_ignora_al_bot():
    assert operator_confirmation([_bot("Tu saldo ya está disponible")]) is False


# --- operator_sent_media (comprobante/tutorial del agente) -------------------

def test_agente_mando_imagen_es_media():
    assert operator_sent_media([_client("quiero retirar"), _agent(media_type="image")]) is True


def test_agente_mando_video_tutorial():
    assert operator_sent_media([_agent("mira este tutorial", media_type="video")]) is True


def test_media_del_cliente_no_cuenta():
    assert operator_sent_media([_client(media_type="image")]) is False


def test_media_type_no_real_no_cuenta():
    # 'chat'/'missed'/'template' NO son media real (un texto guardado como 'chat' no cuenta)
    assert operator_sent_media([_agent(media_type="chat")]) is False
    assert operator_sent_media([_agent(media_type="missed")]) is False
    assert operator_sent_media([_agent(media_type="document")]) is True  # doc sí es media real


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


# --- ACUSE vs ACREDITACION ---------------------------------------------------
# `operator_confirmation` mezcla las dos cosas: "en breve" (voy) y "acreditado"
# (llego). Para la rubrica de deposito hacen falta separadas, porque el 2 estrellas
# es exactamente "acuso pero nunca confirmo la acreditacion". Medido sobre 1.254
# transacciones de deposito: con solo plantillas la falla daba 42,4% y con la
# taquigrafia entera 28,1%; ninguno servia, el primero subcuenta y el segundo
# sobrecuenta por polisemia.

def test_acreditacion_por_token_inequivoco():
    for texto in ("ingresado", "acreditado", "CARGADO", "ya fue abonado",
                  "ya se reflejo en tu cuenta", "ing", "cargó"):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_acreditacion_por_saldo_disponible():
    for texto in ("Tu saldo ya está disponible. Suerte 🍀",
                  "Ya se encuentra disponible su saldo estimado 😊",
                  "¡Gracias por tu recarga, Luis! Tu saldo ya está disponible."):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_disponible_SIN_saldo_no_es_acreditacion():
    # Falsos positivos REALES del dataset: 'disponible' habla de la app o de una promo.
    for texto in ("Por el momento puedes usarla por la web ya que la app aún no está "
                  "disponible, se llama Sorti Reporte",
                  "por el momento no está disponible la app para ios",
                  "si quieres aprovechar la promo que tengo disponible"):
        assert operator_acreditacion([_agent(texto)]) is False, texto


def test_ingreso_sustantivo_no_es_acreditacion():
    # 'Registro o ingreso' = iniciar sesion, no 'se ingreso la plata'.
    assert operator_acreditacion(
        [_agent("Registro o ingreso con los datos que le pase amigo ? jeje")]) is False


def test_listo_solo_cuenta_si_es_un_acuse_seco():
    # "Listo amiga" despues del comprobante es una confirmacion.
    assert operator_acreditacion([_agent("Listo amiga")]) is True
    # "Listo, <nueva instruccion>" no confirma nada: sigue pidiendo cosas.
    assert operator_acreditacion(
        [_agent("Listo, enviame tu usuario para revisar si estas registrado")]) is False
    assert operator_acreditacion(
        [_agent("Listo mi bro, una vez que utilices todo tu saldo real podras usar "
                "el saldo bono")]) is False


def test_el_acuse_NO_es_acreditacion():
    for texto in ("Estamos verificando tu comprobante. Tu recarga se reflejará en breve.",
                  "🔜 Tu solicitud de recarga está siendo procesada",
                  "Permítame un momento"):
        assert operator_acreditacion([_agent(texto)]) is False, texto


def test_acuse_detecta_el_voy():
    for texto in ("Estamos verificando tu comprobante. Tu recarga se reflejará en breve.",
                  "🔜 Tu solicitud de recarga está siendo procesada",
                  "Tu retiro está en proceso 🔄",
                  "Permítame un momento"):
        assert operator_acuse([_agent(texto)]) is True, texto


def test_acuse_no_dispara_con_saludo():
    assert operator_acuse([_agent("Buenos días, ¿en qué te ayudo?")]) is False


def test_acreditacion_ignora_al_cliente_y_al_bot():
    assert operator_acreditacion([_client("ya está acreditado?")]) is False
    assert operator_acreditacion([_bot("Tu saldo ya está disponible")]) is False


def test_acreditacion_respeta_la_negacion():
    assert operator_acreditacion([_agent("todavía no está acreditado")]) is False
    assert operator_acreditacion([_agent("aún no se ha cargado tu saldo")]) is False


# --- operator_asked_anything_else -------------------------------------------
# El criterio NO es "mando la plantilla de despedida" sino "se aseguro de que el
# cliente no necesitara algo mas": la plantilla es una despedida, la pregunta es un
# ofrecimiento. Linea base medida el 2026-08-06: 13,0% de las sesiones. Y no es que
# nadie lo haga — Mario 66% (59 de 89), Andree Rodriguez 0% (0 de 112).

def test_pregunta_si_necesita_algo_mas():
    for texto in ("Paul ¿Hay algo más en lo que te pueda ayudar? 🙂🍀",
                  "¿Alguna otra duda?",
                  "¿En qué más te puedo ayudar?",
                  "¿Necesitas algo más antes de cerrar?",
                  "¿Te quedó alguna inquietud?"):
        assert operator_asked_anything_else([_agent(texto)]) is True, texto


def test_la_despedida_NO_cuenta_como_preguntar():
    # Las plantillas de cierre se despiden; no ofrecen nada.
    for texto in ("Gracias por preferirnos! 🍀💚",
                  "¡Fue un placer atenderte! 😊✨",
                  "Un placer atenderte 😊."):
        assert operator_asked_anything_else([_agent(texto)]) is False, texto


def test_preguntar_algo_mas_ignora_al_cliente():
    assert operator_asked_anything_else([_client("¿algo más que deba hacer?")]) is False


# --- operator_resolved (confirmacion o media del agente) ---------------------

def test_agent_resolved_por_confirmacion():
    assert operator_resolved([_agent("ingresado")]) is True


def test_agent_resolved_por_media():
    assert operator_resolved([_agent(media_type="image")]) is True


def test_agent_no_resolved_solo_saludo():
    assert operator_resolved([_agent("buenas, ¿en qué ayudo?")]) is False


# --- operator_pushed (empuje comercial) --------------------------------------

def test_push_por_link():
    assert operator_pushed([_agent("Regístrate acá https://www.sorti.ec/register?code=1")]) is True


def test_push_por_invitacion_bono_recarga():
    assert operator_pushed([_agent("Recuerda que por tu segunda recarga obtienes un bono del 150%")]) is True


def test_push_te_invito():
    assert operator_pushed([_agent("Te invito a entrar en el siguiente link")]) is True


def test_no_push_solo_informa():
    assert operator_pushed([_agent("El horario de atención es de 9 a 18")]) is False


def test_push_ofrecer_registro_o_promo():
    assert operator_pushed([_agent("¿te creo un usuario?")]) is True
    assert operator_pushed([_agent("tenemos un bono del 100% para vos")]) is True
    assert operator_pushed([_agent("te ayudo a registrarte")]) is True


# --- client_asked_question ------------------------------------------------

def test_client_asked_question_true():
    assert client_asked_question([_client("¿cómo reclamo los 10 giros?")]) is True
    assert client_asked_question([_client("quiero saber cuánto es el mínimo")]) is True


def test_client_asked_question_false_solo_saludo():
    assert client_asked_question([_client("hola"), _client("gracias"), _client("ok")]) is False


# --- operator_maltrato (unico gatillo de 'mala') -----------------------------

def test_maltrato_insulto_explicito():
    assert operator_maltrato([_agent("no seas tonto, ya te expliqué")]) is True


def test_jerga_amistosa_no_es_maltrato():
    # "panita"/"ñaño"/"pana" son trato afectuoso ecuatoriano, NO maltrato
    assert operator_maltrato([_agent("listo mi pana, cualquier cosa avisas ñaño")]) is False


def test_saludo_normal_no_es_maltrato():
    assert operator_maltrato([_agent("Hola, gracias por comunicarte 🙂")]) is False


# --- operator_strong_uplift (empuje CONCRETO para licenciar buena/excelente) --

def test_strong_uplift_link():
    assert operator_strong_uplift([_agent("Registrate acá https://www.sorti.ec/register")]) is True


def test_strong_uplift_imperativo():
    assert operator_strong_uplift([_agent("te invito a depositar y jugar")]) is True
    assert operator_strong_uplift([_agent("depositá ya y activás el bono")]) is True


def test_strong_uplift_pide_datos():
    assert operator_strong_uplift([_agent("pasame tu nombre y cédula para crearte la cuenta")]) is True


def test_plantilla_de_anuncio_NO_es_uplift_concreto():
    # el caso real: 'con tu primer deposito activas...' + 'aprovecha' + 'Anímate' es PISO,
    # no empuje concreto -> no debe licenciar buena/excelente (evita el 5★ de la plantilla).
    assert operator_strong_uplift([_agent(_AD_TEMPLATE)]) is False
    # pero SÍ dispara el push amplio (piso del funnel):
    assert operator_pushed([_agent(_AD_TEMPLATE)]) is True


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


# --- operator_sent_credentials (cuenta creada por el operador) ---------------

def test_credenciales_usuario_y_contrasena():
    assert operator_sent_credentials(
        [_agent("tu usuario: juan123 contraseña: abc456")]) is True


def test_credenciales_tu_usuario_es():
    assert operator_sent_credentials([_agent("tu usuario es juan123")]) is True


def test_credenciales_palabra_credencial():
    assert operator_sent_credentials([_agent("estas son tus credenciales de acceso")]) is True


def test_credenciales_ignora_al_cliente():
    # el CLIENTE mandando "usuario: x" no cuenta (no es el agente entregando el alta)
    assert operator_sent_credentials([_client("usuario: juan123 contraseña: abc456")]) is False


def test_sin_credenciales_saludo_normal():
    assert operator_sent_credentials([_agent("Hola, ¿en qué te ayudo?")]) is False


def test_credenciales_pedir_no_cuenta():
    # el agente PIDE los datos (no los entrega) -> no dispara la regla de contraseña
    assert operator_sent_credentials([_agent("envíame tu usuario y contraseña")]) is False
    assert operator_sent_credentials([_agent("¿cuál es tu usuario?")]) is False
    assert operator_sent_credentials([_agent("pásame tu usuario: ")]) is False


def test_credenciales_etiqueta_sin_valor_no_cuenta():
    # un formulario a completar (etiqueta sin valor en la línea) no es entrega
    assert operator_sent_credentials([_agent("usuario:\ncontraseña:")]) is False


def test_credenciales_pedir_con_dos_puntos_y_valor_no_cuenta():
    # aunque haya etiqueta:valor, el verbo de pedido lo excluye
    assert operator_sent_credentials([_agent("indícame tu usuario: elque tengas")]) is False


# --- operator_sent_register_link (enlace de registro del agente) ------------

def test_register_link_sorti_ec():
    assert operator_sent_register_link([_agent("Regístrate acá sorti.ec/register?code=1")]) is True


def test_register_link_url_generica_con_register():
    assert operator_sent_register_link([_agent("Entra aquí https://otrodominio.com/register")]) is True


def test_register_link_ignora_al_cliente():
    assert operator_sent_register_link([_client("vi sorti.ec/register en otro lado")]) is False


def test_sin_register_link_solo_texto():
    assert operator_sent_register_link([_agent("Puedes registrarte cuando quieras")]) is False


# --- app_mentioned (no hay app; sirve para recomendar la web) -------------

def test_app_mentioned_por_cliente():
    assert app_mentioned([_client("¿tienen app?")]) is True


def test_app_mentioned_por_agente():
    assert app_mentioned([_agent("descarga la aplicación desde la tienda")]) is True


def test_app_mentioned_ignora_notas():
    assert app_mentioned([{"from_me": True, "is_note": True, "body": "revisar app"}]) is False


def test_sin_app_mentioned():
    assert app_mentioned([_client("quiero depositar"), _agent("claro, decime cuanto")]) is False
