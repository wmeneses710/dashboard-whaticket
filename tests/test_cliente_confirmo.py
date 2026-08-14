"""El cliente diciendo que se resolvio es la evidencia mas dura que existe.

EL CASO QUE LO ORIGINO: `060725b4` (motivo `problema`), **1 estrella**. Transcript real:

    [ 0.7m] CLI: Me sale ese error cada q quiero hacer una apuesta
    [ 0.9m] OP : aquí tienes el acceso directo para jugadores: https://www.sorti365.com
    [ 4.7m] CLI: Y una consulta mas ... Mi cuenta ya esta verificada ... ?
    [ 5.6m] OP : Correcto, me indica si le puedo ayudar con algo mas ?
    [12.1m] CLI: Si ya me salio / Todo bien / Muy amable
    [13.1m] OP : Un placer atenderte

El operador contesto en 0,2 y en 0,4 minutos. NO HUBO SILENCIO. Pero:
  - `operator_resolved` da False, porque mandar un LINK no es confirmacion, ni media, ni
    credenciales -- las tres unicas formas que la señal reconoce;
  - `problema` es el UNICO motivo sin rubrica determinista ni piso (`_RESOLVED_FLOOR` /
    `_FUNNEL_FLOOR` no lo incluyen), asi que nada corrige el `atendio=False` del modelo;
  - y `cliente_reinsistio` del LLM enciende `friccion`, que con `atendio=False` da 'mala'.

POR QUE NO ALCANZA CON CORROBORAR `cliente_reinsistio` CONTRA EL SILENCIO. Se probo y se
descarto: con `client_reasked(min_run=2)` esta sesion SIGUE dando True, porque el run que lo
dispara son los tres mensajes de agradecimiento del minuto 12 y los 6,6 minutos previos son
EL CLIENTE PROBANDO EL ARREGLO, no el operador callado. Es el mismo falso positivo que
`MIN_SILENCIO_FRICCION` documenta ("un run incluia 'Listo eso era tdo gracias'"): bajar
`min_run` reintroduce un bug ya aprendido.

EL PATRON ES DELIBERADAMENTE CONSERVADOR, como `MALTRATO_PATTERN`: exige un verbo de
RESOLUCION, no cortesia. "gracias" solo, "listo" solo o "ok" NO confirman nada -- el repo ya
midio en `client_sin_motivo` que la cortesia suelta no significa que el tramite se cumplio.
Y tiene que venir DESPUES de que el operador hablo: si no, es el planteo del cliente
("ya me salio el error de nuevo"), no la confirmacion de un arreglo.
"""
from datetime import datetime, timedelta, timezone

from src.scorer import score_by_motivo
from src.signals import cliente_confirmo_resuelto

BASE = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def _cli(mins, body):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(mins, body="te paso el acceso"):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "OPERATOR"}


# --- lo que SI confirma ----------------------------------------------------------

def test_ya_me_salio_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "me sale un error"), _op(1), _cli(2, "Si ya me salió")]) is True


def test_ya_funciona_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "no me carga"), _op(1), _cli(2, "listo ya funciona gracias")]) is True


def test_ya_pude_ingresar_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "no puedo entrar"), _op(1), _cli(2, "ya pude ingresar")]) is True


def test_se_soluciono_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "tengo un problema"), _op(1), _cli(2, "se solucionó, muchas gracias")]) is True


def test_ya_me_llego_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "no me llega la recarga"), _op(1), _cli(2, "ya me llegó")]) is True


# --- lo que NO confirma (la cortesia suelta no prueba nada) ----------------------

def test_gracias_solo_no_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "tengo un problema"), _op(1), _cli(2, "gracias")]) is False


def test_listo_solo_no_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "tengo un problema"), _op(1), _cli(2, "listo")]) is False


def test_ok_y_muy_amable_no_confirman():
    assert cliente_confirmo_resuelto(
        [_cli(0, "tengo un problema"), _op(1), _cli(2, "ok"), _cli(3, "muy amable")]) is False


def test_ANTES_de_que_el_operador_hable_no_confirma():
    # Es el PLANTEO del cliente, no la confirmacion de un arreglo.
    assert cliente_confirmo_resuelto(
        [_cli(0, "ya me salió ese error otra vez"), _op(1)]) is False


def test_el_operador_diciendolo_no_cuenta():
    # La señal es del CLIENTE: el operador afirmando que ya funciona no es evidencia.
    assert cliente_confirmo_resuelto(
        [_cli(0, "no me carga"), _op(1, "listo, ya funciona todo")]) is False


def test_ya_esta_listo_NO_confirma():
    """El falso positivo que enseño a acotar el patron (caso `d594567c`, registro, 2*).

    El cliente escribio "Ya está listo" a los 19,9 minutos y en el mensaje SIGUIENTE
    aclaro "Estoy esperando su verificación no mas": estaba diciendo "ya hice mi parte",
    no "se resolvio". El verbo tiene que nombrar el DESENLACE, no el estado del cliente.
    """
    assert cliente_confirmo_resuelto(
        [_cli(0, "quiero registrarme"), _op(1, "te ayudo"), _cli(2, "Ya está listo"),
         _cli(3, "Estoy esperando su verificación no mas")]) is False


def test_una_negacion_no_confirma():
    assert cliente_confirmo_resuelto(
        [_cli(0, "no me carga"), _op(1), _cli(2, "no funciona todavía")]) is False
    assert cliente_confirmo_resuelto(
        [_cli(0, "no me carga"), _op(1), _cli(2, "aún no me llegó")]) is False


def test_las_notas_del_crm_no_cuentan():
    notas = [_cli(0, "no me carga"), _op(1),
             {"created_at": BASE + timedelta(minutes=2), "from_me": True, "is_note": True,
              "body": "Anggie *resuelto* la conversación ya funciona", "media_type": None}]
    assert cliente_confirmo_resuelto(notas) is False


# --- el caso real ----------------------------------------------------------------

CONFIRMADA = [
    _cli(0, "me sale un error cada vez que quiero apostar"),
    _op(1, "aquí tienes el acceso directo: https://www.sorti365.com"),
    _cli(2, "Si ya me salió"),
]
SIN_CONFIRMAR = [
    _cli(0, "me sale un error cada vez que quiero apostar"),
    _op(1, "aquí tienes el acceso directo: https://www.sorti365.com"),
    _cli(2, "sigue sin funcionar"),
]


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over):
    resp = {
        "motivo": "problema",
        "dimensions": {"resolucion": "no resolvio", "iniciativa": "nada",
                       "cortesia": "cordial", "errores": []},
        "atendio_el_motivo": False,          # el modelo dice que NO atendio
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "cliente_reinsistio": True,          # y dice que el cliente reinsistio
        "rating_rationale": "no resolvio el problema principal",
        "recomendacion": "ofrece una solucion concreta",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# --- el piso que le faltaba a `problema` -----------------------------------------

def test_la_confirmacion_del_cliente_le_gana_al_atendio_del_modelo():
    # `problema` es el unico motivo sin rubrica determinista ni piso: sin esto, un
    # atendio=False alucinado mas friccion da 'mala' (1 estrella).
    r = score_by_motivo(target_messages=CONFIRMADA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.rating_label != "mala", f"label={r.rating_label} stars={r.stars}"
    assert r.stars >= 3, f"stars={r.stars}"
    assert r.floor_applied is True


def test_la_confirmacion_del_cliente_apaga_la_friccion():
    r = score_by_motivo(target_messages=CONFIRMADA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.friccion is False
    errores = list((r.dimensions or {}).get("errores") or [])
    assert not any("reinsistir" in e.lower() or "repitio" in e.lower() for e in errores), \
        f"errores={errores}"


def test_sin_confirmacion_y_sin_atender_la_nota_sigue_baja():
    """El guard NO amnistia al que no atendio: es una REGRESION si esto cambia.

    Ya no cae a 'mala' porque `cliente_reinsistio` se retiro de `friccion` (ver
    tests/test_reinsistencia_llm.py) y en este fixture no hay silencio medido. Sigue en
    'deficiente' por el `atendio=False` del modelo, que es lo correcto: el piso solo lo
    levanta la confirmacion del CLIENTE, y aca el cliente dijo "sigue sin funcionar".
    """
    r = score_by_motivo(target_messages=SIN_CONFIRMAR, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.rating_label == "deficiente" and r.stars == 2
    assert r.floor_applied is False, "sin confirmacion no se aplica el piso"


def test_la_confirmacion_NO_le_gana_al_silencio_medido():
    """Una señal por regex no puede tumbar un hecho medido con timestamps.

    El cliente escribe 4 veces con 5 minutos de silencio real del operador: la friccion
    EXISTIO, aunque despues la conversacion terminara bien. Caso `d594567c`.
    """
    msgs = [
        _cli(0, "quiero registrarme"),
        _op(1, "ya te ayudo"),
        _cli(7, "hola?"), _cli(8, "alguien ahi"), _cli(9, "me responden"),
        _cli(12, "sigue ahi"),
        _op(13, "perdon, acá estoy"),
        _cli(14, "listo ya funciona"),
    ]
    assert cliente_confirmo_resuelto(msgs) is True, "la confirmacion es real"
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_resp(motivo="registro")))
    assert r.friccion is True, "el silencio medido manda sobre la confirmacion por regex"


def test_la_confirmacion_no_regala_el_cinco():
    # Confirmar que se resolvio prueba que se ATENDIO, no que fue el mejor escenario.
    r = score_by_motivo(target_messages=CONFIRMADA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.stars < 5


def test_el_caso_060725b4_reconstruido():
    msgs = [
        _cli(0.0, "Buenas"),
        _cli(0.7, "Me sale ese error cada q quiero hacer una apuesta"),
        _op(0.8, "Hola, gracias por comunicarte con Atención Al Cliente"),
        _op(0.9, "aquí tienes el acceso directo para jugadores: https://www.sorti365.com"),
        _cli(4.7, "Y una consulta más"),
        _cli(4.8, "Mi cuenta ya está verificada"),
        _cli(5.2, "Pero q me dicen ustedes ?"),
        _op(5.6, "Correcto, me indica si le puedo ayudar con algo mas ?"),
        _cli(12.1, "Si ya me salió"),
        _cli(12.2, "Todo bien"),
        _cli(12.2, "Muy amable"),
        _op(13.1, "Un placer atenderte"),
    ]
    assert cliente_confirmo_resuelto(msgs) is True
