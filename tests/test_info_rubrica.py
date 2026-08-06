"""Tests de src/info.py: rubrica del motivo `info`, 100% DETERMINISTA.

EL EJE lo cerro el negocio el 2026-08-05: "si hay pregunta -> se respondio; si no
hay -> SIN MOTIVO". La segunda mitad de esa regla ya vive en el skip `sin_motivo`
(src/sessions.py), asi que TODA sesion de `info` que llega hasta aca tiene algo que
responder por construccion. Por eso esta rubrica NO lleva detector de preguntas.

Ese fue el error del primer intento: medir con `client_asked_question`, que busca "?"
o palabras interrogativas y se pierde los planteos sin signo — "mas informacion por
favor", "quiero jugar", "estoy interesado". Daba 53,1% de `info` "sin pregunta" y no
era ruido: era el detector mirando lo angosto. El criterio correcto es "hubo algo que
responder", y ese es exactamente el complemento de `sin motivo`.

`info` no tiene comprobante, ni acreditacion, ni material que exigir: es el motivo mas
simple de todos. Lo unico que el operador controla es contestar, y hacerlo rapido.

ESCALA:
    5  respondio <=2 min + se aseguro de que no faltara nada
    4  respondio <=2 min
    3  respondio entre 2 y 5 min
    2  respondio despues de 5 min
    1  no respondio

Umbrales sobre 57 sesiones (1 por persona): mediana 1,5 min, 62,5% <=2 min, 26,8%
entre 2 y 5. El chequeo de cierre esta en 12,3%, asi que el 5 queda exigente y raro.
"""
from datetime import datetime, timedelta, timezone

from src.info import calificar_info, score_info

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

CONSULTA = "¿cuales son los horarios de atencion?"
RESPUESTA = "Atendemos de 6 de la mañana a medianoche, todos los dias."
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


def _cli(minutos, body=CONSULTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body=RESPUESTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_y_chequeando_el_cierre():
    msgs = [_cli(0), _op(1), _op(2, ALGO_MAS)]
    a = calificar_info(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_sin_chequear():
    msgs = [_cli(0), _op(1)]
    a = calificar_info(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_entre_2_y_5_min():
    a = calificar_info([_cli(0), _op(4)])
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_despues_de_5_min():
    a = calificar_info([_cli(0), _op(11)])
    assert a.stars == 2 and a.label == "deficiente"


def test_1_estrella_si_no_respondio():
    a = calificar_info([_cli(0), _cli(6, "hola?")])
    assert a.stars == 1 and a.label == "mala"


# --- el criterio ANCHO: no hace falta un signo de pregunta -------------------

def test_un_planteo_SIN_signo_de_pregunta_se_califica_igual():
    # Strings reales que el detector angosto (`client_asked_question`) perdia y que
    # mandaban el 53,1% de `info` a un limbo de "sin pregunta".
    for planteo in ("mas informacion por favor", "quiero jugar",
                    "estoy interesado", "de q de trata",
                    "hola, estoy escribiendo desde sorti.ec hacen recarga de $1 o $2"):
        a = calificar_info([_cli(0, planteo), _op(1)])
        assert a is not None and a.stars == 4, planteo


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_cli(0), _op(1),
            _op(2, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_info(msgs).stars == 4


def test_el_reloj_arranca_en_el_PLANTEO_del_cliente():
    msgs = [_op(0, "hola! te cuento que soy agente de Sorti365"),
            _cli(30), _op(31)]
    assert calificar_info(msgs).stars == 4


def test_el_bot_no_cuenta_como_respuesta():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": RESPUESTA, "sent_from": "CHATBOT",
           "media_type": "chat"}
    assert calificar_info([_cli(0), bot]).stars == 1


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": CONSULTA, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": RESPUESTA, "media_type": "chat"}]
    assert calificar_info(msgs) is None
    assert score_info(msgs) is None


def test_score_info_devuelve_un_ScoreResult_usable():
    r = score_info([_cli(0), _op(1), _op(2, ALGO_MAS)])
    assert r.motivo == "info"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_del_4_pide_chequear_el_cierre():
    r = score_info([_cli(0), _op(1)])
    assert r.stars == 4 and "algo más" in r.recomendacion.lower()
