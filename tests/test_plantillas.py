"""`sent_from IS NULL` NO es el canal de las respuestas rapidas. Es el de los AUTOMATICOS.

LA PREMISA QUE SE CAYO, Y COMO. Este modulo nacio el 2026-08-20 afirmando que
`sent_from IS NULL` identifica "el mensaje que el operador ELIGIO de las respuestas
rapidas", y con eso se daba por desbloqueado el error critico **E10** ("alterar respuestas
rapidas") y la buena practica **B07** ("usar las respuestas rapidas correctas sin modificar
su contenido"). El razonamiento era estadistico y sonaba bien: el canal NULL repite 120
veces cada texto y el 97,6% de sus mensajes cae en 29 cuerpos distintos, o sea un catalogo.

El 2026-08-28 el ETL empezo a traer el catalogo DE VERDAD (`fast_responses`, 180 filas en
`sistemas`, 178 con texto). Cruzado contra los mensajes, el veredicto es sin ambiguedad:

    mensajes que el CANAL llama plantilla ......... 14.789
    de esos, que matchean una plantilla real ..........  0

Solapamiento CERO. Y lo que hay en el canal NULL, mirando los cuerpos mas repetidos de
30 dias, explica por que:

    'Agente, revisa constantemente nuestro canal oficial...'   6.818   CAMPAÑA
    'Mucha suerte hoy, esperamos poder atenderte de nuevo...'  4.110   farewellMessage
    'Gracias por preferirnos! 🍀💚'                             2.153
    'Gracias por comunicarte con nosotros!...'                 1.208   greetingMessage
    '¡Canjea tus premios del Pronosticador!'                     498   CAMPAÑA

Son los mensajes AUTOMATICOS de la conexion y de la cola, mas las campañas. La evidencia
que se uso para descartar "automatico" -- que el 100% de esas despedidas trae `user_id` --
no probaba lo que se creyo: el CRM le atribuye el farewell al usuario que CIERRA el ticket.

Y las respuestas rapidas de verdad salen por `WEB`, mezcladas con el texto libre. El caso
que lo demuestra en una linea es `/FIN`, que el manual nombra y el catalogo transcribe:
`{{contactTreatment}} ¿Hay algo más en lo que te pueda ayudar? 🙂🍀`. El canal NO la ve.

NO HUBO DAÑO EN LAS NOTAS: ninguna rubrica consume este modulo todavia (medido: el unico
importador era este test). Lo que se corrige es el NOMBRE, antes de que E10 se construya
encima.

LO QUE SIGUE SIENDO CIERTO, y no se toca:
  * `CHATBOT` y `api` son maquinas de verdad: 7.148 mensajes (0,6%), sin un solo `user_id`.
  * `metrics.sin_persona_detras` excluye NULL a proposito y **hace bien** -- pero por la
    razon CONTRARIA a la que estaba escrita: no porque haya una persona eligiendo de una
    lista, sino porque el mensaje lo dispara el ticket y el CRM se lo firma a alguien.
  * un mensaje del canal NULL a los 2 segundos no es un reloj falso.

PARA MEDIR E10 hace falta el catalogo (`fast_responses`) y SIMILITUD, no este canal y no un
booleano: un mensaje ALTERADO por construccion no matchea su plantilla, asi que "no
matchea" no distingue "la altero" de "escribio libre".
"""
from datetime import datetime, timedelta, timezone

from src.metrics import sin_persona_detras
from src.plantillas import (
    CANAL_AUTOMATICO,
    es_del_operador,
    es_mensaje_automatico,
    uso_de_canales,
)

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
UID = "0147c434-8d56-4a25-a81a-74fa15e1b480"

# Textos REALES, de los cuerpos mas repetidos del canal NULL (30 dias).
DESPEDIDA = ("Mucha suerte hoy, esperamos poder atenderte de nuevo, pronto!🍀🎉\n"
             "Recuerda que siempre tenemos un numero alterno para que siempre puedas "
             "comunicarte 5**********2")
SALUDO = "Gracias por comunicarte con nosotros! Siempre es un placer atenderte!🍀🎉"
CAMPANA = ("Agente, revisa constantemente nuestro canal oficial de WhatsApp. "
           "*Hoy comenzó el torneo de casino y allí compartimos todos los detalles*")
# Texto REAL de la respuesta rapida `/FIN`, tal cual la trae `fast_responses`, ya con el
# placeholder sustituido como sale al aire.
FIN = "Estimado ¿Hay algo más en lo que te pueda ayudar? 🙂🍀"


def _msg(minutos, *, from_me=True, sent_from="WEB", user_id=UID, body="hola",
         is_note=False):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": from_me,
            "is_note": is_note, "body": body, "sent_from": sent_from,
            "user_id": user_id, "media_type": "chat"}


# --- EL TEST QUE ENCIERRA EL HALLAZGO -------------------------------------------------

def test_la_respuesta_rapida_REAL_no_sale_por_el_canal_automatico():
    """EL test de esta correccion. `/FIN` es una respuesta rapida del catalogo y viaja por
    `WEB`: si algun dia alguien vuelve a leer el canal como "eligio de una lista", esto
    falla. Ver el docstring del modulo: solapamiento CERO sobre 14.789 mensajes."""
    fin = _msg(0, sent_from="WEB", body=FIN)
    assert es_mensaje_automatico(fin) is False, (
        "el canal esta clasificando una respuesta rapida real como automatica: esa es "
        "exactamente la premisa que se cayo el 2026-08-28"
    )
    assert es_del_operador(fin) is True


def test_el_canal_automatico_son_despedida_saludo_y_campana():
    """Los tres cuerpos que DOMINAN el canal NULL en la data real."""
    for texto in (DESPEDIDA, SALUDO, CAMPANA):
        m = _msg(1, sent_from=CANAL_AUTOMATICO, body=texto)
        assert es_mensaje_automatico(m) is True, f"no reconocio el automatico: {texto[:40]!r}"
        assert es_del_operador(m) is False


def test_lo_que_sale_por_WEB_es_del_operador():
    """`WEB` mezcla texto libre CON respuestas rapidas, y el canal no los separa. El nombre
    dice lo que se puede afirmar: salio del operador."""
    assert es_del_operador(_msg(0, sent_from="WEB")) is True
    assert es_mensaje_automatico(_msg(0, sent_from="WEB")) is False


def test_la_maquina_no_es_ninguna_de_las_dos():
    """`CHATBOT` y `api` no tienen persona detras; los cubre `sin_persona_detras`."""
    for remitente in ("CHATBOT", "api"):
        m = _msg(2, sent_from=remitente, user_id=None)
        assert es_mensaje_automatico(m) is False, f"{remitente} no es el canal automatico"
        assert es_del_operador(m) is False, f"{remitente} no es del operador"


def test_el_mensaje_del_cliente_no_es_de_ningun_canal_del_negocio():
    """La pregunta es sobre el trabajo del OPERADOR. El cliente no manda nada de esto."""
    cli = _msg(4, from_me=False, sent_from=None)
    assert es_mensaje_automatico(cli) is False
    assert es_del_operador(cli) is False


def test_la_nota_interna_no_cuenta():
    """La nota del CRM es `from_me` pero NO es un mensaje al cliente. Misma leccion que
    `cliente_tuvo_la_ultima_palabra` y `sin_respuesta`."""
    nota = _msg(5, sent_from=None, is_note=True)
    assert es_mensaje_automatico(nota) is False
    assert es_del_operador(nota) is False


def test_uso_de_canales_cuenta_las_dos_mitades():
    """La pregunta del negocio siempre es relativa: tres automaticos y nada propio no es lo
    mismo que un automatico y cinco frases suyas."""
    msgs = [
        _msg(0, sent_from=CANAL_AUTOMATICO, body=SALUDO),
        _msg(1, sent_from="WEB", body="permitame un momento"),
        _msg(2, sent_from="WEB", body=FIN),
        _msg(3, sent_from=CANAL_AUTOMATICO, body=DESPEDIDA),
        _msg(4, sent_from="CHATBOT", user_id=None),          # maquina: no entra
        _msg(5, from_me=False, sent_from=None),              # cliente: no entra
        _msg(6, sent_from=None, is_note=True),               # nota: no entra
    ]
    assert uso_de_canales(msgs) == (2, 2)


def test_no_contradice_sin_persona_detras():
    """Son preguntas DISTINTAS y las dos siguen valiendo: `sin_persona_detras` responde
    '¿lo mando una maquina?' y este modulo '¿por que canal salio?'. El canal automatico
    tiene `user_id`, asi que no es 'sin persona' -- el CRM se lo firma a quien cerro."""
    # `sin_persona_detras` recibe UN mensaje (lo decide el remitente), no una lista.
    despedida = _msg(0, sent_from=CANAL_AUTOMATICO, body=DESPEDIDA)
    assert sin_persona_detras(despedida) is False
    assert es_mensaje_automatico(despedida) is True
    # Y al reves con la maquina: `sin_persona_detras` SI, canal automatico NO.
    bot = _msg(1, sent_from="CHATBOT", user_id=None)
    assert sin_persona_detras(bot) is True
    assert es_mensaje_automatico(bot) is False
