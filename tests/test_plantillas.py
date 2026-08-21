"""El canal de respuestas rapidas ya esta en la data: es `sent_from IS NULL`.

POR QUE ESTO DESBLOQUEA ALGO QUE ESTABA TRABADO. El manual de ATC nombra sus respuestas
rapidas catorce veces (`/Bienvenida`, `/FIN`, `/R5Placer`, `/Visto`...) y NO transcribe el
texto de ninguna. Sin ese texto, dos cosas eran inmedibles: el error critico **E10**
("alterar respuestas rapidas, protocolos o informacion oficial") y la buena practica **B07**
("utilizar las respuestas rapidas correctas sin modificar su contenido"). La conclusion
hasta hoy era "hay que pedirle los textos al negocio".

No hace falta: el CRM ya nos dice por que CANAL salio cada mensaje.

MEDIDO el 2026-08-20 sobre los mensajes del negocio (`from_me`, sin notas):

    sent_from      mensajes    con user_id    cuerpos distintos
    WEB           1.169.762      1.169.612              373.814   <- escrito a mano
    NULL            240.006        234.452                1.998   <- respuesta rapida
    CHATBOT           5.981              0                   32   <- maquina
    api               1.167              0                  271   <- difusion masiva

Son poblaciones que no se tocan. `WEB` promedia 3,1 mensajes por cuerpo distinto; el canal
NULL promedia **120**. Y de sus 240.006 mensajes, **234.159 (97,6%)** caen en apenas 29
textos repetidos 200+ veces -- entre ellos los tres `farewellMessage` que el propio payload
del CRM publica como oficiales.

LO QUE NO ES: automatico. Cuesta ver la diferencia y me la lleve por delante una vez. Las
14.706 despedidas del canal NULL tienen `user_id` en **14.704 (100%)**: las manda el
OPERADOR tocando la respuesta rapida, no el CRM solo. Por eso `metrics.sin_persona_detras`
excluye NULL a proposito y hace bien -- y por eso un mensaje de plantilla a los 2 segundos
NO es un reloj falso: es el operador cumpliendo lo que el manual le pide ("/Bienvenida, para
no pasarse del minuto mientras se arma la respuesta").

MAQUINA de verdad son solo `CHATBOT` y `api`: 7.148 mensajes (0,6%), sin un solo `user_id`,
y ya cubiertos por `sin_persona_detras`.
"""
from datetime import datetime, timedelta, timezone

from src.metrics import sin_persona_detras
from src.plantillas import CANAL_PLANTILLA, es_plantilla, uso_plantillas

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
UID = "0147c434-8d56-4a25-a81a-74fa15e1b480"


def _msg(minutos, *, from_me=True, sent_from="WEB", user_id=UID, body="hola",
         is_note=False):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": from_me,
            "is_note": is_note, "body": body, "sent_from": sent_from,
            "user_id": user_id, "media_type": "chat"}


def test_lo_escrito_a_mano_no_es_plantilla():
    """`WEB` es el operador tecleando: 373.814 cuerpos distintos."""
    assert es_plantilla(_msg(0, sent_from="WEB")) is False


def test_el_canal_nulo_es_la_respuesta_rapida():
    """El operador toco una respuesta rapida: sale sin `sent_from` pero CON su user_id."""
    m = _msg(1, sent_from=CANAL_PLANTILLA,
             body="Mucha suerte hoy, esperamos poder atenderte de nuevo, pronto!")
    assert es_plantilla(m) is True


def test_el_chatbot_no_es_una_respuesta_rapida():
    """Es una MAQUINA, no un operador eligiendo una plantilla. 0 de 5.981 con user_id."""
    m = _msg(2, sent_from="CHATBOT", user_id=None)
    assert es_plantilla(m) is False
    assert sin_persona_detras(m) is True


def test_la_difusion_masiva_no_es_una_respuesta_rapida():
    """`api` es marketing masivo: 0 de 1.167 con user_id."""
    m = _msg(3, sent_from="api", user_id=None,
             body="*Comunicado* Estimados agentes, les informamos que...")
    assert es_plantilla(m) is False
    assert sin_persona_detras(m) is True


def test_el_mensaje_del_cliente_nunca_es_plantilla():
    """La pregunta es sobre el trabajo del OPERADOR. El cliente no manda plantillas."""
    assert es_plantilla(_msg(4, from_me=False, sent_from=None)) is False


def test_la_nota_interna_no_es_plantilla():
    """La nota del CRM es `from_me` pero no es un mensaje al cliente. Misma leccion que
    `cliente_tuvo_la_ultima_palabra`."""
    assert es_plantilla(_msg(5, sent_from=None, is_note=True)) is False


def test_uso_plantillas_cuenta_solo_las_del_operador():
    msgs = [
        _msg(0, from_me=False, sent_from=None, body="Hola"),      # cliente
        _msg(1, sent_from=CANAL_PLANTILLA, body="/Bienvenida"),   # plantilla
        _msg(2, sent_from="WEB", body="ya te ayudo"),             # a mano
        _msg(3, sent_from=CANAL_PLANTILLA, body="/FIN"),          # plantilla
        _msg(4, sent_from="CHATBOT", user_id=None),               # maquina
        _msg(5, sent_from=None, is_note=True),                    # nota
    ]
    plantillas, a_mano = uso_plantillas(msgs)
    assert plantillas == 2
    assert a_mano == 1


def test_sin_mensajes_no_rompe():
    assert uso_plantillas([]) == (0, 0)
