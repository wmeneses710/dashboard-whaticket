"""Tests de src/deposito.py: rubrica del motivo `deposito`, 100% DETERMINISTA.

Todo PURO, en memoria, sin LLM y sin BD. El motivo lo sigue clasificando el modelo;
lo que se determina aca es la NOTA, porque los tres hechos que la definen son
verificables: el reloj, si confirmo la acreditacion, y si chequeo que no faltara nada.

ESCALA (definida por el negocio el 2026-08-06; para deposito "con que se haga bien y
rapido es suficiente", y el comprobante se exige por AUDITORIA y proteccion de la
confianza, no como metrica de satisfaccion):
    5  acuse <=2 min + confirmo la acreditacion + se aseguro de que no faltara nada
    4  acuse <=2 min + confirmo la acreditacion
    3  confirmo, pero el acuse tardo 2-5 min
    2  el acuse tardo >5 min, o nunca confirmo la acreditacion
    1  ni respondio ni confirmo

Umbrales calibrados sobre 1.254 transacciones (1 por persona, jul-ago 2026):
el 78,0% acusa en <=2 min y el 76,2% confirma en <=5 min del comprobante.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.deposito import calificar_deposito, score_deposito

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _comprobante(minutos):
    return _cli(minutos, body="", media="image")


def _op(minutos, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


ACUSE = "Estamos verificando tu comprobante. Tu recarga se reflejara en breve."
ACREDITA = "Gracias por tu recarga. Tu saldo ya esta disponible."
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


# --- el corte transaccion / consulta ----------------------------------------

def test_sin_comprobante_del_cliente_NO_es_una_transaccion():
    # 64,6% de las sesiones con contexto de recarga son CONSULTAS: preguntan por la
    # recarga sin hacer ninguna. No hay nada que acreditar -> esta rubrica no aplica
    # y devuelve None para que decida el caller.
    msgs = [_cli(0, "como hago para recargar?"), _op(1, "por transferencia bancaria")]
    assert calificar_deposito(msgs) is None
    assert score_deposito(msgs) is None


def test_el_comprobante_del_cliente_activa_la_rubrica():
    msgs = [_cli(0, "les mando el comprobante de la recarga"), _comprobante(0),
            _op(1, ACUSE), _op(3, ACREDITA)]
    assert calificar_deposito(msgs) is not None


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_confirmado_y_chequeo_que_no_faltara_nada():
    msgs = [_cli(0, "recarga"), _comprobante(0),
            _op(1, ACUSE), _op(3, ACREDITA), _op(4, ALGO_MAS)]
    a = calificar_deposito(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_y_confirmado_pero_cerro_sin_preguntar():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_confirmo_pero_el_acuse_tardo_entre_2_y_5():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(4, ACUSE), _op(6, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_el_acuse_tardo_mas_de_5_aunque_haya_confirmado():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(9, ACUSE), _op(11, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_2_estrellas_respondio_rapido_pero_NUNCA_confirmo_la_acreditacion():
    # El caso que el detector viejo dejaba pasar: "en breve" y desaparece.
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE)]
    a = calificar_deposito(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_1_estrella_no_respondio_nada():
    msgs = [_cli(0, "recarga"), _comprobante(0)]
    a = calificar_deposito(msgs)
    assert a.stars == 1 and a.label == "mala"


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    # El bug que destapo sacar el cap: el 37,1% de los 5 estrellas se ganaban SOLO
    # por ser amables. Ser amable no es lograr el mejor escenario del motivo.
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA),
            _op(4, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    a = calificar_deposito(msgs)
    assert a.stars == 4, "la despedida cordial no puede valer un 5"


def test_el_bot_no_cuenta_como_respuesta_del_operador():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": ACUSE, "sent_from": "CHATBOT",
           "media_type": "chat"}
    msgs = [_cli(0, "recarga"), _comprobante(0), bot]
    a = calificar_deposito(msgs)
    assert a.stars == 1


def test_las_notas_internas_no_cuentan():
    nota = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
            "is_note": True, "body": ACREDITA, "media_type": "chat"}
    msgs = [_cli(0, "recarga"), _comprobante(0), nota]
    assert calificar_deposito(msgs).stars == 1


def test_el_reloj_arranca_en_el_COMPROBANTE_no_en_el_primer_mensaje():
    # El cliente saluda, charla, y 30 min despues manda el comprobante. El operador
    # responde 1 min despues de ESO: es un 4, no se le imputa la charla previa.
    msgs = [_cli(0, "buenas, queria hacer una recarga"), _op(1, "buenas, dale"),
            _comprobante(30), _op(31, ACUSE), _op(32, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 4


def test_sin_created_at_no_revienta_y_cede_el_turno():
    # `fetch_messages` (path por conversacion) NO trae created_at; solo lo trae
    # `fetch_session_messages`. Es la misma trampa documentada en src/context.py, que
    # ya habia reventado la rubrica de agilidad contra la BD. Sin reloj no hay nota que
    # dar: se devuelve None y decide el caller, en vez de explotar con KeyError.
    msgs = [{"from_me": False, "is_note": False, "body": "les mando la recarga",
             "media_type": "chat"},
            {"from_me": False, "is_note": False, "body": "", "media_type": "image"},
            {"from_me": True, "is_note": False, "body": ACREDITA, "media_type": "chat"}]
    assert calificar_deposito(msgs) is None
    assert score_deposito(msgs) is None


def test_score_deposito_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA),
            _op(4, ALGO_MAS)]
    r = score_deposito(msgs)
    assert r.motivo == "deposito"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == "", "en el mejor escenario no hay nada que recomendar"


def test_la_recomendacion_dice_QUE_falto_para_el_5():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA)]
    r = score_deposito(msgs)
    assert r.stars == 4
    assert "algo mas" in r.recomendacion.lower() or "algo más" in r.recomendacion.lower()
