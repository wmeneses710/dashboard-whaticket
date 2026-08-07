"""La pregunta de cierre solo cuenta si ADEMAS se esperó la respuesta.

MEDIDO el 2026-08-07 sobre 2.493 sesiones: 280 mandaron la pregunta de cierre, y de esas
**193 (69%) cerraron el ticket en menos de UN MINUTO**. Mediana de espera antes de cerrar:
**0,0 minutos**; p75 = 0,1. Solo 29 esperaron más de 5 minutos.

O sea que se escribe la pregunta y se cierra en el mismo instante: el cliente no tiene
ventana para una segunda duda. Y cuatro rúbricas deterministas (deposito, retiro, info,
soporte) daban crédito de uplift SOLO por haberla escrito.

Regla del negocio: el mínimo a esperar son 5 MINUTOS, y el efecto queda confinado al paso
4 -> 5 (sin espera real se topa en `buena`; con ella se alcanza `excelente`).
"""
from datetime import datetime, timedelta, timezone

from src.signals import operator_asked_and_waited

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)
PREGUNTA = "¿Hay algo más en lo que te pueda ayudar? 🙂🍀"


def _op(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


def _cli(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


BASICO = [_cli(0, "me acreditas la recarga"), _op(30, "listo, ing"), _op(40, PREGUNTA)]


def test_preguntar_y_cerrar_al_instante_NO_cuenta():
    # El caso de las 193: pregunta y cierra el ticket en el mismo minuto.
    cierre = BASE + timedelta(seconds=45)
    assert operator_asked_and_waited(BASICO, cierre) is False


def test_esperar_los_5_minutos_SI_cuenta():
    cierre = BASE + timedelta(seconds=40 + 300)
    assert operator_asked_and_waited(BASICO, cierre) is True


def test_cuatro_minutos_no_alcanza():
    cierre = BASE + timedelta(seconds=40 + 240)
    assert operator_asked_and_waited(BASICO, cierre) is False


def test_si_el_cliente_CONTESTO_esperó_por_definicion():
    # 68 de las 280 medidas. Si hubo respuesta, la ventana existió: no hace falta el reloj.
    msgs = BASICO + [_cli(60, "no, gracias")]
    assert operator_asked_and_waited(msgs, BASE + timedelta(seconds=65)) is True


def test_sin_la_pregunta_no_hay_credito():
    msgs = [_cli(0, "me acreditas"), _op(30, "listo, ing")]
    assert operator_asked_and_waited(msgs, BASE + timedelta(hours=1)) is False


def test_sin_dato_de_cierre_NO_se_quita_el_credito():
    # Falla del lado que no castiga: sin la hora de cierre no se puede probar que no
    # esperó, y hoy hay caminos que no la traen. Misma regla que la fricción sin relojes.
    assert operator_asked_and_waited(BASICO, None) is True


def test_sin_created_at_en_la_pregunta_tampoco_se_castiga():
    msgs = [{"from_me": True, "is_note": False, "body": PREGUNTA, "sent_from": "OPERATOR"}]
    assert operator_asked_and_waited(msgs, BASE + timedelta(seconds=10)) is True


def test_el_umbral_es_configurable():
    cierre = BASE + timedelta(seconds=40 + 120)
    assert operator_asked_and_waited(BASICO, cierre) is False
    assert operator_asked_and_waited(BASICO, cierre, min_espera=timedelta(minutes=1)) is True
