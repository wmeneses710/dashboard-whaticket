"""Un sticker no es un comprobante.

`MEDIA_TYPES` incluia `sticker`, y `is_real_media` es la FUENTE UNICA de "esto es un adjunto
de verdad" para cuatro preguntas distintas. Con el sticker adentro, las cuatro se contestaban
mal:

    es_pedido (agilidad)     un sticker del agente era "un pedido que hay que confirmar"
    operator_sent_media      un sticker del operador contaba como mandar el comprobante
    client_sin_motivo        una sesion donde el cliente solo manda un sticker era calificable
    _hubo_intento (soporte)  un sticker del operador contaba como haber hecho algo

MEDIDO el 2026-08-17: 7 de las 439 filas de agilidad en 1 estrella tienen como unico pedido
abandonado un sticker. El numero es chico; la direccion del error no: la nota dice que el
operador dejo sin responder un comprobante que nunca existio.

El sticker es el emoji de WhatsApp. Un emoji ya lo trata `es_cortesia` (que devuelve True
cuando el texto normalizado no deja ninguna palabra), y esto lo alinea: da lo mismo mandar
"👍" como texto o como sticker.
"""
from datetime import datetime, timedelta, timezone

from src.agilidad import es_pedido
from src.sessions import evaluate_session
from src.signals import (
    client_sin_motivo,
    is_real_media,
    operator_sent_media,
)

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


def test_el_sticker_no_es_un_adjunto_de_verdad():
    assert is_real_media("sticker") is False


def test_los_adjuntos_de_verdad_no_se_tocan():
    for tipo in ("image", "video", "audio", "voice", "ptt", "document",
                 "application", "viewonce", "image/jpeg"):
        assert is_real_media(tipo) is True, tipo


def test_un_sticker_del_agente_no_es_un_pedido():
    """Las 7 filas de agilidad 1★ que acusaban de no confirmar un sticker."""
    assert es_pedido([_cli(0, "", media="sticker")]) is False


def test_un_sticker_con_texto_de_cortesia_tampoco():
    assert es_pedido([_cli(0, "", media="sticker"), _cli(0, "gracias")]) is False


def test_un_comprobante_sigue_siendo_un_pedido():
    assert es_pedido([_cli(0, "", media="image")]) is True


def test_el_sticker_del_operador_no_cuenta_como_mandar_el_comprobante():
    assert operator_sent_media([_op(1, "", media="sticker")]) is False
    assert operator_sent_media([_op(1, "", media="image")]) is True


def test_un_sticker_con_un_gracias_es_una_sesion_sin_motivo():
    """Mismo criterio que el emoji en texto: `client_sin_motivo` ya saltea "🙌🏻"."""
    msgs = [_op(0, "hola! te cuento que soy agente de Sorti365"),
            _cli(1, "", media="sticker"),
            _cli(1, "gracias"),
            _op(2, "te creo la cuenta?")]
    assert client_sin_motivo(msgs) is True
    assert evaluate_session(msgs)[2:] == ("skipped", "sin_motivo")


def test_un_sticker_pelado_tampoco_deja_nota():
    """SIN texto la sesion no llega a `sin_motivo` -- `client_sin_motivo` mira el TEXTO del
    cliente y ahi no hay ninguno -- pero se saltea igual, por `customer_media_only`. Lo que
    importa es que no salga una nota: la causa que muestra el tablero es discutible y no es
    una acusacion."""
    msgs = [_op(0, "hola! te cuento que soy agente de Sorti365"),
            _cli(1, "", media="sticker"),
            _op(2, "te creo la cuenta?")]
    assert evaluate_session(msgs)[2:] == ("skipped", "customer_media_only")


def test_un_cliente_que_manda_un_comprobante_SI_planteo_algo():
    msgs = [_op(0, "hola"), _cli(1, "gracias", media="image")]
    assert client_sin_motivo(msgs) is False
    assert evaluate_session(msgs)[2:] == ("evaluated", None)
