"""Tests de src/retiro.py: rubrica del motivo `retiro`, 100% DETERMINISTA.

Todo PURO, en memoria, sin LLM y sin BD. Espeja a src/deposito.py, con la asimetria
que define el motivo: en deposito el comprobante lo manda el CLIENTE y el operador
debe CONFIRMAR; en retiro el comprobante lo manda el OPERADOR y es la entrega misma.

ESCALA (definida por el negocio el 2026-08-06; "con que se haga bien y rapido es
suficiente", y el comprobante se exige por AUDITORIA y proteccion de la confianza):
    5  respuesta <=2 min + comprobante <=15 min + se aseguro de que no faltara nada
    4  respuesta <=2 min + comprobante <=15 min
    3  respuesta 2-5 min, o comprobante 15-30 min
    2  respondio pero nunca mando el comprobante, o tardo de mas
    1  ni respondio ni mando comprobante

Umbrales calibrados sobre 108 transacciones de retiro (1 sesion por persona,
jul-ago 2026): 74,1% responde en <=2 min y el 86,1% manda el comprobante dentro de
los 15 min del pedido.
"""
from datetime import datetime, timedelta, timezone

from src.retiro import calificar_retiro, es_transaccion, score_retiro

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

FORMULARIO = ("Monto a retirar: 30 Nombres: Alan Apellidos: Montaño "
              "Cedula: 0951964055 Banco: Guayaquil")


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


def _comprobante_op(minutos):
    return _op(minutos, body="", media="image")


ACUSE = "Tu retiro esta en proceso 🔄. En breve te enviaremos el comprobante."
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


# --- el corte transaccion / consulta ----------------------------------------

def test_preguntar_por_el_retiro_SIN_monto_no_es_transaccion():
    # 56,8% de las sesiones de retiro son CONSULTAS. No hay plata pedida, no hay
    # comprobante que mandar: calificarlas con la vara transaccional castiga al
    # operador por algo que nunca ocurrio.
    for pregunta in ("¿como hago para retirar?", "cuando me pagan la comision?",
                     "se puede retirar los domingos?"):
        assert es_transaccion([_cli(0, pregunta), _op(1, "por transferencia")]) is False, pregunta


def test_el_monto_convierte_la_sesion_en_transaccion():
    assert es_transaccion([_cli(0, "quiero retirar 30"), _op(1, ACUSE)]) is True
    assert es_transaccion([_cli(0, FORMULARIO), _op(1, ACUSE)]) is True


def test_la_cedula_y_el_telefono_NO_se_confunden_con_el_monto():
    # Viajan en el mismo formulario: son corridas de 10 digitos, no plata.
    assert es_transaccion([_cli(0, "mi cedula es 0951964055 para el retiro")]) is False
    assert es_transaccion([_cli(0, "mi numero 0986987466, quiero retirar")]) is False


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_con_comprobante_y_chequeo_de_cierre():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(8), _op(9, ALGO_MAS)]
    a = calificar_retiro(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_con_comprobante_pero_sin_chequear():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(8)]
    a = calificar_retiro(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_si_la_respuesta_tardo_entre_2_y_5():
    msgs = [_cli(0, FORMULARIO), _op(4, ACUSE), _comprobante_op(9)]
    a = calificar_retiro(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_3_estrellas_si_el_comprobante_tardo_entre_15_y_30():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(22)]
    a = calificar_retiro(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_si_NUNCA_mando_el_comprobante():
    # El caso que el negocio quiere ver: "en breve te lo enviamos" y nunca llega.
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE)]
    a = calificar_retiro(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_2_estrellas_si_el_comprobante_tardo_mas_de_30():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(45)]
    a = calificar_retiro(msgs)
    assert a.stars == 2


def test_2_estrellas_si_la_respuesta_tardo_mas_de_5():
    msgs = [_cli(0, FORMULARIO), _op(9, ACUSE), _comprobante_op(12)]
    a = calificar_retiro(msgs)
    assert a.stars == 2


def test_1_estrella_si_no_respondio_nada():
    msgs = [_cli(0, FORMULARIO)]
    a = calificar_retiro(msgs)
    assert a.stars == 1 and a.label == "mala"


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(8),
            _op(9, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_retiro(msgs).stars == 4


def test_el_reloj_arranca_en_el_PEDIDO_no_en_el_saludo():
    # El cliente saluda, charla 30 min y recien ahi pide el retiro.
    msgs = [_cli(0, "buenas"), _op(1, "buenas, decime"),
            _cli(30, FORMULARIO), _op(31, ACUSE), _comprobante_op(38)]
    assert calificar_retiro(msgs).stars == 4


def test_el_comprobante_del_CLIENTE_no_cuenta_como_entrega():
    # En retiro la entrega la hace el OPERADOR. Una imagen del cliente no acredita nada.
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _cli(8, "", media="image")]
    assert calificar_retiro(msgs).stars == 2


def test_el_bot_no_cuenta_como_respuesta():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": ACUSE, "sent_from": "CHATBOT",
           "media_type": "chat"}
    msgs = [_cli(0, FORMULARIO), bot]
    assert calificar_retiro(msgs).stars == 1


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": FORMULARIO, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": ACUSE, "media_type": "chat"}]
    assert calificar_retiro(msgs) is None
    assert score_retiro(msgs) is None


def test_score_retiro_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE), _comprobante_op(8), _op(9, ALGO_MAS)]
    r = score_retiro(msgs)
    assert r.motivo == "retiro"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_dice_QUE_falto():
    msgs = [_cli(0, FORMULARIO), _op(1, ACUSE)]
    r = score_retiro(msgs)
    assert r.stars == 2 and "comprobante" in r.recomendacion.lower()


# --- LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL PEDIDO ---------------------------
# Mismo arreglo que en deposito: la frontera es la nota de cierre del operador (ver
# src/interacciones.py). En las 5.624 conversaciones con varios cierres -- el 3,51%, donde
# viven el 41,7% de los mensajes -- el comprobante de entrega de un retiro acreditaba el
# pedido de OTRO. Caso `e5607f47-0387-46ac-a754-b1f90bb8a28b`: mezcla retiros y depositos del
# 5 al 8-ago y la nota (4★) usaba solo el primer pedido con evidencia de los siguientes.

def _cierre_nota(minutos, quien="Mel"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def test_el_comprobante_de_OTRA_interaccion_no_entrega_este_retiro():
    msgs = [_cli(0, "Monto a retirar: 70"), _op(1, "Tu retiro está en proceso"),
            _cierre_nota(5),
            _cli(2880, "Monto a retirar: 30"), _op(2881, "listo, acá tienes", media="image"),
            _cierre_nota(2882)]
    r = calificar_retiro(msgs)
    assert r is not None
    assert r.entrega is None, "la entrega del 2do retiro no puede cerrar el 1er pedido"
    assert r.stars == 2, f"{r.stars}★ {r.rationale}"


def test_sin_cierres_la_ventana_sigue_siendo_toda_la_sesion():
    # No-regresion del 96,3% de las conversaciones.
    msgs = [_cli(0, "Monto a retirar: 70"), _op(1, "dale"),
            _op(2, "acá tienes", media="image")]
    r = calificar_retiro(msgs)
    assert r is not None and r.entrega is not None
