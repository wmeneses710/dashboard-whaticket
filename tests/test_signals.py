"""Tests de las senales deterministas de resolucion (src/signals.py).

Son la capa que corrige la dureza sistematica del LLM: detecta -sin modelo- que
el agente SI atendio el motivo (confirmo una transaccion o mando el comprobante),
para que el scorer no lo hunda por debajo del piso. Mensajes = dicts con
from_me, is_note, body, media_type, sent_from.
"""
from datetime import datetime, timedelta, timezone

from src.signals import (
    operator_acreditacion,
    operator_confirmation,
    operator_maltrato,
    operator_pushed,
    operator_resolved,
    operator_sent_credentials,
    operator_sent_media,
    app_mentioned,
    client_asked_question,
    client_reasked,
)

# La plantilla real del flujo de anuncio (pitch-only): NO es empuje concreto.
_AD_TEMPLATE = (
    "Debes registrarte, verificar tu cuenta y con tu primer deposito activas todas las "
    "promociones. No te pierdas la promo, aprovechala. Anímate y me avisas.")


_BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)


def _agent(body="", media_type=None, at=None):
    m = {"from_me": True, "is_note": False, "body": body, "media_type": media_type}
    if at is not None:
        m["created_at"] = _BASE + timedelta(seconds=at)
    return m


def _client(body="", media_type=None, at=None):
    """`at` = segundos desde una base fija. Necesario desde que la friccion exige relojes."""
    m = {"from_me": False, "is_note": False, "body": body, "media_type": media_type}
    if at is not None:
        m["created_at"] = _BASE + timedelta(seconds=at)
    return m


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


# Hallado el 2026-08-12 auditando el rescore v5: la plantilla personal de una operadora
# no matcheaba NINGUN patron de acreditacion. No tiene 'disponible' cerca de 'saldo', no
# cae en _ACREDITA_FUERTE_RE, y su "Listo" viene pegado al signo de exclamacion asi que
# _LISTO_RE (anclado en ^) tampoco lo agarra. El sistema la trataba como si nunca hubiera
# confirmado la acreditacion.
# TAMAÑO MEDIDO: 106 sesiones de `deposito` en 2 estrellas "nunca confirmo", y CONCENTRADAS
# en una sola persona -- 122 de sus 296 sesiones en 2 estrellas (41,2%) contra el 10,1% y
# el 11,0% de sus pares. Su tasa real de "nunca confirmo" es ~5,7%: el hueco de vocabulario
# le estaba construyendo un desempeño que no era el suyo.

def test_acreditacion_por_saldo_que_ya_se_puede_usar():
    for texto in ("¡Listo! 🎉✨ Ya puedes disfrutar tu saldo. Mucha suerte 🍀",
                  "Ya puedes usar tu saldo",
                  "ya puede utilizar su saldo, mucha suerte"):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_poder_usar_algo_que_NO_es_el_saldo_no_acredita():
    # El mismo cuidado que ya se tuvo con 'disponible': el verbo solo vale si habla del
    # SALDO, no de la app ni de una promo.
    for texto in ("ya puedes usar la app sin problema",
                  "ya puedes disfrutar de todas las promociones"):
        assert operator_acreditacion([_agent(texto)]) is False, texto


def test_prometer_que_va_a_poder_usar_el_saldo_no_acredita():
    # Espeja _FUTURO_RE: la promesa es ACUSE, no acreditacion.
    assert operator_acreditacion(
        [_agent("en breve ya puedes disfrutar tu saldo")]) is False


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


def test_acreditacion_ignora_al_cliente_y_al_bot():
    assert operator_acreditacion([_client("ya está acreditado?")]) is False
    assert operator_acreditacion([_bot("Tu saldo ya está disponible")]) is False


def test_acreditacion_respeta_la_negacion():
    assert operator_acreditacion([_agent("todavía no está acreditado")]) is False
    assert operator_acreditacion([_agent("aún no se ha cargado tu saldo")]) is False


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


# --- client_reasked (fricción determinista) -------------------------------

def test_reasked_corrida_larga_sin_respuesta():
    # El cliente manda 5 seguidos sin respuesta del negocio -> fricción, PERO desde el
    # 2026-08-07 tambien hace falta SILENCIO REAL (>=5 min): medido, el 50,6% de esta rama
    # eran 4+ mensajes en menos de un minuto, donde nadie pudo haber contestado.
    msgs = [_client("hola", at=0), _client("necesito ayuda", at=60),
            _client("me sale error", at=200), _client("hola?", at=320),
            _client("?", at=400)]
    assert client_reasked(msgs) is True


def test_reasked_corrida_larga_pero_INMEDIATA_no_es_friccion():
    # Misma corrida en 40 segundos: es como escribe la gente, no insistencia.
    msgs = [_client("hola", at=0), _client("necesito ayuda", at=8),
            _client("me sale error", at=20), _client("hola?", at=31),
            _client("?", at=40)]
    assert client_reasked(msgs) is False


def test_reasked_pings_de_desesperacion_en_corrida():
    # corrida corta pero con pings claros ("ayuda", "?") DESPUES de esperar -> fricción
    msgs = [_client("hice un deposito", at=0), _client("ayuda", at=400),
            _client("?", at=600)]
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


# --- app_mentioned (no hay app; sirve para recomendar la web) -------------

def test_app_mentioned_por_cliente():
    assert app_mentioned([_client("¿tienen app?")]) is True


def test_app_mentioned_por_agente():
    assert app_mentioned([_agent("descarga la aplicación desde la tienda")]) is True


def test_app_mentioned_ignora_notas():
    assert app_mentioned([{"from_me": True, "is_note": True, "body": "revisar app"}]) is False


def test_sin_app_mentioned():
    assert app_mentioned([_client("quiero depositar"), _agent("claro, decime cuanto")]) is False


# --- EL VOCABULARIO DE ACREDITACION SE ESCAPA POR EL TEXTO LIBRE -------------------
# Tercera vez que aparece la misma familia de hueco: el patron se armo desde las PLANTILLAS
# ("tu saldo ya esta disponible") y las confirmaciones que el operador escribe a mano no
# entran. Ya paso con "ya puedes disfrutar tu saldo" (106 sesiones, arreglado el 2026-08-12
# por la mañana) y con esto, hallado leyendo los 2 estrellas de produccion:
#   'Tu saldo ya está en tu cuenta compita'   -> el operador SI confirmo, la nota decia que no
#   'ya te lo cargué'   ('cargo' se reconocia y 'cargué' no: el acento rompia `carg[oó]`)
#   'ya lo tienes en tu cuenta' · 'ya esta realizado'
# VOLUMEN MEDIDO: 76 conversaciones con "saldo ... en tu cuenta", 117 con "ya te lo cargue"
# y ~371 con "ya esta realizado / ya se proceso".
# El FALSO POSITIVO a evitar son solo 3 mensajes: "su verificacion ya esta realizada" — ahi
# lo realizado es el tramite, no la plata.

def test_acreditacion_por_saldo_en_la_cuenta():
    for texto in ("Tu saldo ya está en tu cuenta compita. Éxitos!!",
                  "Su saldo ya se encuentra en su cuenta",
                  "ya lo tienes en tu cuenta amigo"):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_acreditacion_en_primera_persona():
    # 'ya le cargo' se reconocia y 'ya te lo cargué' no, por el acento.
    for texto in ("ya te lo cargué", "ya le cargue mi pana", "ya lo acredité"):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_acreditacion_por_operacion_realizada():
    for texto in ("ya esta realizado amigo", "ya se realizo mi amigo", "ya quedó realizado"):
        assert operator_acreditacion([_agent(texto)]) is True, texto


def test_un_TRAMITE_realizado_no_es_una_acreditacion():
    # Lo realizado es la verificacion/solicitud, no la plata. Son 3 mensajes en la base.
    for texto in ("Estimado su verificacion ya esta realizada",
                  "tu solicitud ya está realizada, ahora espera la aprobación"):
        assert operator_acreditacion([_agent(texto)]) is False, texto


def test_el_acuse_EN_CURSO_sigue_sin_ser_acreditacion():
    # Guard: "esta siendo procesada" (en curso) NO es lo mismo que "ya se proceso" (hecho).
    for texto in ("Tu solicitud de recarga está siendo procesada",
                  "ya se esta procesando amigo", "tu recarga está en proceso"):
        assert operator_acreditacion([_agent(texto)]) is False, texto
