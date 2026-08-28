"""Canales de salida del negocio: distinguir el AUTOMATICO del que escribio el operador.

CORREGIDO EL 2026-08-28. Este modulo nacio el 2026-08-20 afirmando que `sent_from IS NULL`
identifica "el mensaje que el operador ELIGIO de las respuestas rapidas", y con eso se daba
por desbloqueado el error critico **E10** ("alterar respuestas rapidas") y la buena practica
**B07**. El razonamiento era estadistico y sonaba solido: el canal NULL repite 120 veces cada
texto y el 97,6% de sus mensajes cae en 29 cuerpos distintos, o sea un catalogo.

ERA UN CATALOGO, PERO NO ESE. El 2026-08-28 el ETL empezo a traer el catalogo de verdad
(tabla `fast_responses`: 180 filas en `sistemas`, 178 con texto). Cruzado contra los
mensajes, el veredicto no admite lectura alternativa:

    mensajes que el CANAL llama plantilla ......... 14.789
    de esos, que matchean una plantilla real ..........  0

Solapamiento CERO. Y los cuerpos que dominan el canal NULL en 30 dias explican por que:

    'Agente, revisa constantemente nuestro canal oficial...'   6.818   CAMPAÑA
    'Mucha suerte hoy, esperamos poder atenderte de nuevo...'  4.110   farewellMessage
    'Gracias por preferirnos! 🍀💚'                             2.153
    'Gracias por comunicarte con nosotros!...'                 1.208   greetingMessage
    '¡Canjea tus premios del Pronosticador!'                     498   CAMPAÑA

Son los AUTOMATICOS de la conexion y de la cola, mas las campañas. La evidencia que se uso
para descartar "automatico" -- que el 100% de esas despedidas trae `user_id` -- no probaba lo
que se creyo: el CRM le atribuye el farewell al usuario que CIERRA el ticket.

Las respuestas rapidas de verdad salen por `WEB`, mezcladas con el texto libre, y el canal no
las separa. El caso que lo prueba en una linea es `/FIN`, que el manual nombra y el catalogo
transcribe: `{{contactTreatment}} ¿Hay algo más en lo que te pueda ayudar? 🙂🍀`.

POR ESO LOS NOMBRES CAMBIARON. `es_plantilla` -> `es_mensaje_automatico` y
`es_escrito_a_mano` -> `es_del_operador`: el segundo tampoco era cierto, porque `WEB` trae
texto libre Y respuestas rapidas juntos. Cada nombre dice ahora lo unico que el canal permite
afirmar. NO HUBO DAÑO EN LAS NOTAS: ninguna rubrica consumia este modulo (el unico importador
era su test), y por eso la correccion se pudo hacer completa en vez de por capas.

LO QUE SIGUE SIENDO CIERTO Y NO SE TOCA:
  * `CHATBOT` y `api` son maquinas de verdad: 7.148 mensajes (0,6%), sin un solo `user_id`.
  * `metrics.sin_persona_detras` excluye NULL a proposito y **hace bien** -- pero por la razon
    CONTRARIA a la que estaba escrita aca: no porque haya alguien eligiendo de una lista, sino
    porque el mensaje lo dispara el ticket y el CRM se lo firma a alguien. Las dos preguntas
    son distintas ("¿lo mando una maquina?" contra "¿por que canal salio?") y las dos valen.
  * un mensaje del canal automatico a los 2 segundos NO es un reloj falso.

PARA MEDIR E10 hace falta el catalogo (`fast_responses`) y SIMILITUD, no este canal y no un
booleano: un mensaje ALTERADO por construccion no matchea su plantilla, asi que "no matchea"
no distingue "la altero" de "escribio libre". Eso va en su propio modulo, con el patron
`build_*_map` de src/redireccion.py, porque es un agregado sobre el corpus y no una funcion
pura -- y tiene que respetar `fast_responses.updated_at`: el catalogo es el estado de HOY y
los mensajes son historicos.
"""
from __future__ import annotations

# El valor de `sent_from` del canal AUTOMATICO (saludo de la cola, despedida de la conexion,
# campañas). Es None de verdad: el CRM no escribe nada en la columna cuando el mensaje no
# salio de la sesion de un operador.
CANAL_AUTOMATICO = None

# Remitentes que son una MAQUINA. Espeja `metrics._REMITENTES_SIN_PERSONA` a proposito en vez
# de importarlo: ese set responde "hay una persona detras?" y este "por que canal salio?".
# Si algun dia se agrega un remitente automatico nuevo hay que tocar los dos, y el test
# `test_la_maquina_no_es_ninguna_de_las_dos` los ata con un assert sobre ambos.
_MAQUINAS = frozenset({"CHATBOT", "api"})


def _es_del_negocio(message: dict) -> bool:
    """Mensaje del negocio dirigido al cliente. La nota interna del CRM es `from_me` pero
    NO es un mensaje al cliente -- misma leccion que `cliente_tuvo_la_ultima_palabra`."""
    return bool(message.get("from_me")) and not message.get("is_note")


def es_mensaje_automatico(message: dict) -> bool:
    """El mensaje lo disparo el CRM: saludo de cola, despedida de conexion o campaña.

    Lo decide el CANAL (`sent_from`), que es lo unico observable. NO significa que no haya
    una persona: el CRM le firma la despedida al operador que cierra el ticket. Y NO
    significa "respuesta rapida" -- esas salen por `WEB` (ver el docstring del modulo).
    """
    if not _es_del_negocio(message):
        return False
    remitente = message.get("sent_from")
    if remitente in _MAQUINAS:
        return False
    return remitente is CANAL_AUTOMATICO


def es_del_operador(message: dict) -> bool:
    """El mensaje salio de la sesion del operador (canal `WEB`).

    Mezcla el texto que TECLEO con las respuestas rapidas que ELIGIO, y el canal no los
    separa: para eso hace falta el catalogo `fast_responses`, no esta funcion.
    """
    if not _es_del_negocio(message):
        return False
    remitente = message.get("sent_from")
    return remitente is not CANAL_AUTOMATICO and remitente not in _MAQUINAS


def uso_de_canales(messages: list[dict]) -> tuple[int, int]:
    """(cuantos automaticos salieron, cuantos escribio el operador).

    Las dos mitades juntas porque la pregunta del negocio siempre es relativa: mandar tres
    automaticos y nada propio no es lo mismo que mandar uno y cinco frases suyas.
    Las maquinas (`CHATBOT`, `api`) no entran en ninguna de las dos.
    """
    automaticos = sum(1 for m in messages if es_mensaje_automatico(m))
    del_operador = sum(1 for m in messages if es_del_operador(m))
    return automaticos, del_operador
