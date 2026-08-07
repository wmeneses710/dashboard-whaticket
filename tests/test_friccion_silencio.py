"""La friccion exige SILENCIO REAL del operador, no solo mensajes seguidos.

MEDIDO el 2026-08-07 sobre 3.000 sesiones (30 dias). `client_reasked` disparaba en el
15,0% y bajaba la nota en 108 sesiones (3,6%), pero:
  - **50,6%** de la rama por conteo (run>=4) eran 4+ mensajes del cliente en MENOS DE UN
    MINUTO, y **79,1%** de la rama del ping. Nadie podia haber contestado todavia.
  - solo **74 de 449 disparos (16,5%)** tenian un span mayor a 5 minutos.
  - la duplicacion de mensajes, que era la sospecha inicial del negocio, explicaba apenas
    el 2,2% (10 de 449 morian al deduplicar). El confusor grande era otro: el 23,2% eran
    RAFAGAS de mensajes cortos, o sea como escribe la gente. Un run incluia
    "Listo eso era tdo gracias" — un cliente satisfecho contado como que reinsistio.

Insistir significa que el operador TUVO TIEMPO de responder y no lo hizo. Sin medir el
silencio, la señal castigaba a quien atendio a alguien que escribe rapido, y `friccion`
siempre demota a 'deficiente': eran 2 estrellas injustas.
"""
from datetime import datetime, timedelta, timezone

from src.signals import client_reasked

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)


def _cli(seg, body=""):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(seg, body="ya te ayudo"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


# --- lo que NO es friccion --------------------------------------------------------

def test_una_rafaga_de_mensajes_cortos_no_es_friccion():
    # El caso medido: 4 mensajes en 40 segundos. Es como escribe la gente.
    msgs = [_cli(0, "Recargueme"), _cli(12, "En la cuenta agente"),
            _cli(25, "LIZANDRO"), _cli(40, "por favor")]
    assert client_reasked(msgs) is False


def test_un_ping_inmediato_tampoco():
    # "?" a los 20 segundos no es haber sido ignorado.
    msgs = [_cli(0, "hola necesito ayuda con mi deposito"), _cli(20, "?")]
    assert client_reasked(msgs) is False


def test_el_cliente_satisfecho_no_reinsiste():
    # Run real de produccion que la señal vieja marcaba como friccion.
    msgs = [_cli(0, "Ok"), _cli(9, "Listo eso era tdo gracias"),
            _cli(30, "Ola q tl"), _cli(48, "Puedo hacer un deposito")]
    assert client_reasked(msgs) is False


def test_mensajes_duplicados_no_alcanzan():
    # La sospecha original del negocio: el mismo texto repetido.
    msgs = [_cli(0, "¿Cómo activo mi cuenta?"), _cli(5, "¿Cómo activo mi cuenta?"),
            _cli(11, "¿Cómo activo mi cuenta?"), _cli(16, "Si")]
    assert client_reasked(msgs) is False


# --- lo que SI es friccion -------------------------------------------------------

def test_esperar_mas_de_5_minutos_e_insistir_SI_es_friccion():
    msgs = [_cli(0, "Hola buenas noches por favor me ayuda con el comprobante"),
            _cli(420, "Sigo sin poder subir el comprobante")]
    assert client_reasked(msgs) is True


def test_cuatro_mensajes_a_lo_largo_de_media_hora_SI():
    msgs = [_cli(0, "me ayudas con una recarga"), _cli(600, "hola"),
            _cli(1200, "alguien"), _cli(1800, "me responden")]
    assert client_reasked(msgs) is True


def test_una_respuesta_del_operador_CORTA_la_corrida():
    # Ya estaba asi y tiene que seguir: cada respuesta del negocio reinicia la corrida Y
    # el reloj del silencio. Aca el operador contesta a los 60s y el cliente insiste dos
    # veces dentro de los 3 minutos siguientes -> no llego a haber silencio.
    msgs = [_cli(0, "me ayudas?"), _op(60), _cli(100, "sigues ahi"), _cli(160, "?")]
    assert client_reasked(msgs) is False


def test_el_silencio_se_mide_contra_el_ULTIMO_mensaje_del_operador():
    # El cliente escribe, el operador contesta, y DESPUES lo deja colgado 20 minutos.
    msgs = [_cli(0, "hola"), _op(30), _cli(60, "necesito retirar"),
            _cli(1300, "me responden por favor")]
    assert client_reasked(msgs) is True


# --- degradacion sin timestamps --------------------------------------------------

def test_sin_created_at_NO_dispara():
    # Falla del lado que no castiga: sin relojes no se puede afirmar que hubo silencio,
    # y una demotion a 'deficiente' sin evidencia es peor que perder la señal.
    # Solo afecta al path por-conversacion (scripts/), que no trae created_at; el worker
    # scorea por SESION y ahi los timestamps siempre vienen.
    msgs = [{"from_me": False, "is_note": False, "body": "me responden"},
            {"from_me": False, "is_note": False, "body": "?"},
            {"from_me": False, "is_note": False, "body": "alguien ahi"},
            {"from_me": False, "is_note": False, "body": "ayuda"}]
    assert client_reasked(msgs) is False


def test_el_umbral_es_configurable():
    msgs = [_cli(0, "me ayudas con una recarga"), _cli(120, "?")]
    assert client_reasked(msgs) is False
    assert client_reasked(msgs, min_silencio=timedelta(minutes=1)) is True
