"""Respuestas rapidas: distinguir lo que el operador ELIGIO de lo que ESCRIBIO.

EL CANAL LO DICE EL CRM, no hace falta el texto canonico. El manual de ATC nombra sus
respuestas rapidas catorce veces y no transcribe ninguna, asi que E10 ("alterar respuestas
rapidas") y B07 ("usar las respuestas rapidas correctas sin modificar su contenido") estaban
inmedibles. No lo estan: `messages.sent_from` separa las dos poblaciones.

MEDIDO el 2026-08-20 sobre los mensajes del negocio (`from_me`, sin notas):

    sent_from      mensajes    con user_id    cuerpos distintos    msgs por cuerpo
    WEB           1.169.762      1.169.612              373.814                3,1
    NULL            240.006        234.452                1.998              120,1
    CHATBOT           5.981              0                   32              186,9
    api               1.167              0                  271                4,3

El canal NULL repite 120 veces cada texto y el 97,6% de sus mensajes cae en 29 cuerpos
distintos -- entre ellos los tres `farewellMessage` que el payload del CRM publica como
oficiales. Eso es un catalogo de plantillas, no gente escribiendo.

NULL NO ES "AUTOMATICO", Y CONFUNDIRLO CUESTA CARO. Las 14.706 despedidas del canal NULL
tienen `user_id` en 14.704 (100%): las manda el OPERADOR tocando la respuesta rapida. De ahi
salen dos conclusiones que parecen bugs y no lo son:
  - una plantilla a los 2 segundos NO es un reloj falso; es el operador haciendo lo que el
    manual le pide ("/Bienvenida, para no pasarse del minuto mientras se arma la respuesta");
  - una sesion que cierra con la despedida de plantilla NO evade el E06: el operador cerro.
Por eso `metrics.sin_persona_detras` excluye NULL a proposito, y este modulo NO lo
contradice: son preguntas distintas -- "lo mando una maquina?" contra "lo eligio de una
lista?".

LO QUE ESTE MODULO NO HACE TODAVIA: decidir si una plantilla fue ALTERADA (el E10 completo).
Eso necesita el catalogo de los cuerpos del canal NULL, que es un agregado sobre el corpus y
no una funcion pura -- va como `build_*_map` (ver src/redireccion.build_lineas_map), con su
propio test. Aca queda solo la señal por mensaje, que es lo que todas las rubricas van a
consultar.

EL 2,4% QUE QUEDA AFUERA: 5.847 mensajes del canal NULL no caen en ningun texto repetido
200+ veces. Pueden ser plantillas de baja frecuencia o texto libre que salio por ese canal.
La señal los cuenta como plantilla porque el CANAL es lo que se observa; si alguna vez
importa distinguirlos, hace falta el catalogo y un umbral, no otro campo.
"""
from __future__ import annotations

# El valor de `sent_from` del canal de respuestas rapidas. Es None de verdad: el CRM no
# escribe nada en la columna cuando el mensaje sale de la lista de plantillas.
CANAL_PLANTILLA = None

# Remitentes que son una MAQUINA. Espeja `metrics._REMITENTES_SIN_PERSONA` a proposito en vez
# de importarlo: ese set responde "hay una persona detras?" y este "lo eligio de una lista?".
# Si algun dia se agrega un remitente automatico nuevo hay que tocar los dos, y el test
# `test_el_chatbot_no_es_una_respuesta_rapida` los ata con un assert sobre ambos.
_MAQUINAS = frozenset({"CHATBOT", "api"})


def _es_del_negocio(message: dict) -> bool:
    """Mensaje del negocio dirigido al cliente. La nota interna del CRM es `from_me` pero
    NO es un mensaje al cliente -- misma leccion que `cliente_tuvo_la_ultima_palabra`."""
    return bool(message.get("from_me")) and not message.get("is_note")


def es_plantilla(message: dict) -> bool:
    """El operador mando este mensaje ELIGIENDOLO de las respuestas rapidas.

    Lo decide el CANAL (`sent_from`), no el texto: no hace falta conocer la plantilla para
    saber que salio de la lista.
    """
    if not _es_del_negocio(message):
        return False
    remitente = message.get("sent_from")
    if remitente in _MAQUINAS:
        return False
    return remitente is CANAL_PLANTILLA


def es_escrito_a_mano(message: dict) -> bool:
    """El operador TECLEO este mensaje (canal `WEB`), no lo eligio de una lista."""
    if not _es_del_negocio(message):
        return False
    remitente = message.get("sent_from")
    return remitente is not CANAL_PLANTILLA and remitente not in _MAQUINAS


def uso_plantillas(messages: list[dict]) -> tuple[int, int]:
    """(cuantas respuestas rapidas mando, cuantos mensajes escribio a mano).

    Las dos mitades juntas porque la pregunta del negocio siempre es relativa: mandar tres
    plantillas y nada propio no es lo mismo que mandar una plantilla y cinco frases suyas.
    Las maquinas (`CHATBOT`, `api`) no entran en ninguna de las dos.
    """
    plantillas = sum(1 for m in messages if es_plantilla(m))
    a_mano = sum(1 for m in messages if es_escrito_a_mano(m))
    return plantillas, a_mano
