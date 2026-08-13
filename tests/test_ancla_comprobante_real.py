"""El ancla de `deposito` tiene que elegir un COMPROBANTE, no la última imagen que pasó.

`_comprobantes_del_cliente` filtra por `is_real_media` y nada más, y `_comprobante_del_cliente`
se queda con la última. Como una sesión mergea todos los episodios del ticket, CUALQUIER imagen
posterior del cliente —una foto, un pantallazo de un error— se vuelve el ancla, la ventana
juzgada salta a esa interacción, y la rúbrica pregunta "¿confirmó la acreditación?" sobre una
conversación donde nunca hubo un depósito.

`es_transaccion` YA exige contexto de recarga, pero lo hace sobre la SESIÓN ENTERA: alcanza con
que haya habido un depósito real más atrás para que una imagen sin relación quede habilitada
como ancla. La corroboración es de sesión y la elección es de interacción — ese es el hueco.

LOS DOS CASOS REALES (auditoría del 2026-08-13, los dos de la misma operadora):
  `0a61513b`  6 imágenes del cliente. El ancla eligió una del 13-ago con caption VACÍO cuya
              interacción es "Buenos días / ¿Hay algún problema con la página?" — un problema
              de login. Los depósitos reales que ella SÍ confirmó estaban en interacciones
              anteriores. Nota: 2 estrellas, "nunca le confirmó que la plata había entrado".
  `23ff3128`  197 mensajes y 100 imágenes candidatas. El ancla eligió una con caption
              "Buenos días ING presente en la finca MARÍA MARÍA" — una foto de una finca.
              Nota: 2 estrellas, mismo rationale.

Hay un tercero conocido, `1f53cdc6`: dos recargas confirmadas por otros dos operadores y, seis
días después, una imagen sobre una pregunta de apuestas que se volvió el ancla — el 2 estrellas
cayó sobre quien no tuvo ningún depósito en su turno.

LA REGLA: el ancla es la ÚLTIMA imagen que se pueda corroborar como comprobante EN SU PROPIA
interacción, con las dos puertas que la rúbrica ya usa (el cliente da contexto de recarga, o el
operador acusa el comprobante recibido). Si ninguna se corrobora, no hay transacción que juzgar
y la sesión cede el turno al pase con LLM, que es lo que ya hace cuando no hay comprobante.
"""
from datetime import datetime, timedelta, timezone

from src.deposito import _comprobante_del_cliente, es_transaccion, score_deposito

BASE = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


def _cli(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


def _nota(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": True, "body": body, "media_type": None, "sent_from": None}


DIA = 24 * 3600

# Un depósito REAL y bien atendido, y días después una visita que no tiene nada que ver.
SESION_DEL_CASO = [
    _cli(0, "les mando el comprobante de la recarga"),
    _cli(5, "", media="image"),
    _op(20, "recibido"),
    _op(60, "listo, tu saldo ya está disponible"),
    _nota(90, "Anya Alexandra *resuelto* la conversación"),
    # --- seis días después, otra visita: un problema de la página ---
    _cli(6 * DIA, "Buenos días"),
    _cli(6 * DIA + 30, "Hay algún problema con la página?"),
    _cli(6 * DIA + 60, "", media="image"),          # pantallazo del error, NO un comprobante
    _op(6 * DIA + 120, "Buenos días Carlos 😉"),
    _nota(6 * DIA + 300, "Anya Alexandra *resuelto* la conversación"),
]


def test_el_ancla_no_elige_una_imagen_sin_relacion_con_la_recarga():
    ancla = _comprobante_del_cliente(SESION_DEL_CASO)
    assert ancla is not None
    assert ancla["created_at"] == BASE + timedelta(seconds=5), \
        "el ancla saltó a la imagen del problema de la página"


def test_la_nota_describe_el_deposito_real_y_no_el_silencio_ajeno():
    s = score_deposito(SESION_DEL_CASO, BASE + timedelta(seconds=6 * DIA + 300))
    assert s is not None
    assert s.stars >= 4, s.rating_rationale
    assert "nunca le confirmó" not in s.rating_rationale


def test_una_sesion_donde_NINGUNA_imagen_se_corrobora_no_es_transaccion():
    # La foto de la finca: hay imagen, pero nada la respalda como comprobante. Sin
    # transacción que juzgar, la sesión cede el turno al pase con LLM.
    msgs = [
        _cli(0, "Buenos días ING presente en la finca MARÍA MARÍA"),
        _cli(5, "", media="image"),
        _op(60, "Hola, gracias por comunicarte con nosotros"),
    ]
    assert es_transaccion(msgs) is False
    assert score_deposito(msgs, BASE + timedelta(seconds=600)) is None


def test_el_comprobante_SIN_CAPTION_sigue_valiendo_si_el_operador_lo_acusa():
    # EL GUARD que evita que el arreglo rompa el caso modal: en producción el caption de
    # 33.914 comprobantes es VACÍO, y la corroboración la da el operador al acusarlo.
    msgs = [
        _cli(0, "", media="image"),
        _op(30, "recibido el comprobante, ya lo cargo"),
        _op(60, "listo, tu saldo ya está disponible"),
    ]
    assert es_transaccion(msgs) is True
    s = score_deposito(msgs, BASE + timedelta(seconds=600))
    assert s is not None and s.stars >= 4, s.rating_rationale


def test_la_sesion_de_un_solo_deposito_no_cambia():
    msgs = [
        _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(20, "recibido"), _op(60, "listo, tu saldo ya está disponible"),
    ]
    s = score_deposito(msgs, BASE + timedelta(seconds=600))
    assert s is not None and s.stars >= 4, s.rating_rationale
