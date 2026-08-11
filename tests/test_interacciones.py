"""La frontera de una interaccion sale del OBJETO: la nota de cierre del operador.

Criterio del negocio (2026-08-11): "una interaccion es cuando un usuario habla con el
operador, es este quien decide cuando cerrar, esto deberia estar en el objeto". Y estaba:
el CRM escribe "<Nombre> *resuelto* la conversacion" como nota interna (409.820 de esas).
Nadie lo veia porque TODAS las rubricas filtran `is_note` en su primera linea.
"""
from datetime import datetime, timedelta, timezone

from src.interacciones import es_cierre, interaccion_de, partir_en_interacciones

BASE = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)


def _cli(min_, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(min_, body):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


def _cierre(min_, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def _asignado(min_, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": True, "body": f"*Asignado automáticamente* a {quien}"}


def test_reconoce_la_nota_de_cierre_y_solo_esa():
    assert es_cierre(_cierre(0)) is True
    assert es_cierre(_asignado(0)) is False
    assert es_cierre({"is_note": True, "body": "Mario *aceptado* la conversación"}) is False
    assert es_cierre({"is_note": True, "body": "Mario *reabierto* la conversación"}) is False
    # un mensaje REAL que diga la palabra no es un cierre: la frontera es la NOTA
    assert es_cierre(_op(0, "ya lo dejo *resuelto*")) is False


def test_sin_cierre_todo_es_UNA_interaccion():
    # El 96,3% de las conversaciones cae aca: un solo cierre (o ninguno) = una interaccion.
    msgs = [_cli(0, "me recarga"), _op(1, "listo")]
    assert len(partir_en_interacciones(msgs)) == 1


def test_cada_cierre_abre_una_interaccion_nueva():
    msgs = [_cli(0, "me recarga"), _op(1, "acreditado"), _cierre(2),
            _cli(60, "otra recarga"), _op(61, "acreditado"), _cierre(62)]
    inter = partir_en_interacciones(msgs)
    assert len(inter) == 2
    assert inter[0][0]["body"] == "me recarga"
    assert inter[1][0]["body"] == "otra recarga"


def test_una_interaccion_sin_mensajes_reales_no_cuenta():
    # Arranca con la asignacion y cierra sin que nadie hable: no es una interaccion.
    msgs = [_asignado(0), _cierre(1),
            _cli(10, "me recarga"), _op(11, "acreditado"), _cierre(12)]
    inter = partir_en_interacciones(msgs)
    assert len(inter) == 1
    assert inter[0][0]["body"] == "me recarga"


def test_las_notas_viajan_dentro_de_la_interaccion():
    # No se le saca informacion a nadie: quien filtra `is_note` es cada rubrica.
    msgs = [_asignado(0), _cli(1, "hola"), _op(2, "listo"), _cierre(3)]
    assert len(partir_en_interacciones(msgs)[0]) == 4


def test_interaccion_de_acota_la_busqueda_al_comprobante_correcto():
    # EL CASO `f9b31f4f`: un comprobante que nadie contesto, y la acreditacion de OTRA
    # transaccion dias despues. Sin acotar, se emparejaban.
    huerfano = _cli(0, "", media="image")
    msgs = [huerfano, _cierre(5),
            _cli(4800, "", media="image"), _cli(4801, "me recarga"),
            _op(4802, "gracias por tu recarga, tu saldo ya esta disponible"), _cierre(4803)]
    ventana = interaccion_de(msgs, huerfano)
    assert any(m is huerfano for m in ventana)
    cuerpos = " ".join((m.get("body") or "") for m in ventana)
    assert "saldo ya esta disponible" not in cuerpos, \
        "la acreditacion de otra interaccion no puede caer en esta ventana"


def test_un_ancla_desconocida_degrada_al_transcript_completo():
    msgs = [_cli(0, "hola"), _cierre(1)]
    assert interaccion_de(msgs, _cli(999, "no estoy")) is msgs
