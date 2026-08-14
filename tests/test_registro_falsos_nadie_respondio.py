"""Tres arreglos de `registro`, los tres del mismo caso: la fila dice "nadie le respondió"
sobre operadores que respondieron.

Los trajo el negocio el 2026-08-14 mirando el tablero, con tres sesiones reales.

1. YA TENIA CUENTA (`9f0f0717`, Genessis, 1★). El cliente pide inscribirse, el operador le
   dice -- correctamente-- que ya tiene cuenta, y la rúbrica lo juzga por un alta que era
   IMPOSIBLE: "no ofreció ni guio el proceso de registro". La rama del rechazo ya existe
   (`_RECHAZO_RE`), pero vive SOLO en el camino determinista y esta sesión no llega ahí
   (`es_transaccion=False`). MEDIDO: 19 de 2.451 filas del camino LLM (0,8%).

2. EL NUMERO QUE NO ERA UNA CEDULA (`23049219`, Salome, 1★). `_CEDULA_RE` es `\\b\\d{10}\\b`
   y un celular ecuatoriano tambien tiene 10 digitos. El operador pidio el WhatsApp, el
   cliente mando `0999367608`, y ESO se volvio el ancla del alta -> la ventana se corrio al
   episodio equivocado y quedaron 0 mensajes del operador despues. La cedula real
   (`1501055956`) estaba en el otro episodio, donde el operador SI respondio dos veces.
   Es el mismo guard que ya tiene `_FORM_BANCARIO_RE`: mirar el mensaje ANTERIOR del
   operador.

3. LA SESION ENTREGO CREDENCIALES Y LA VENTANA NO LAS VE (`caa27f9a`, Genessis, 1★). 86
   mensajes, 6 interacciones, `operator_sent_credentials=True` sobre la sesión: el operador
   entrego "Usuario:vicentenava / Contraseña: Sorti123", un link y un video. El ancla-ultima
   eligio la cola de la conversación y la nota salió 1★ "nunca recibió su usuario y clave".
   **La fila SABE que la cuenta se creó y afirma lo contrario.** MEDIDO: 5 de 66 filas de
   registro determinista en 1-2 estrellas.
"""
from datetime import datetime, timedelta, timezone

from src.registro import calificar_registro, es_cedula_de_alta
from src.scorer import score_by_motivo

BASE = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def _cli(mins, body):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(mins, body):
    return {"created_at": BASE + timedelta(minutes=mins), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "OPERATOR"}


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over):
    resp = {
        "motivo": "registro",
        "dimensions": {"resolucion": "le dijo que ya tenia cuenta", "iniciativa": "nada",
                       "cortesia": "cordial", "errores": []},
        "atendio_el_motivo": False,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "cliente_reinsistio": False,
        "rating_rationale": "no ofrecio ni guio el proceso de registro",
        "recomendacion": "ofrece el registro paso a paso",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# --- 1) el cliente YA TENIA cuenta, por el camino LLM ------------------------------

YA_TENIA_CUENTA = [
    _cli(0, "como se puede inscribir confirmen"),
    _op(3, "ya tienes una cuenta amigo de la plataforma de sorti"),
    _cli(11, "Quiero inscribirme"),
    _op(18, "ya tienes cuenta pana, es de pronosticos deportivos y casino"),
]


def test_no_se_castiga_un_alta_que_era_IMPOSIBLE():
    r = score_by_motivo(target_messages=YA_TENIA_CUENTA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.rating_label != "mala", f"label={r.rating_label} stars={r.stars}"
    assert r.stars >= 3, f"stars={r.stars}"
    assert r.floor_applied is True


def test_el_texto_dice_que_la_cuenta_YA_EXISTIA():
    r = score_by_motivo(target_messages=YA_TENIA_CUENTA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert "ya tenía una cuenta" in r.rating_rationale or "ya tenia una cuenta" in r.rating_rationale


def test_sin_rechazo_la_nota_no_se_toca():
    # El guard NO amnistia al que simplemente no atendio: es una REGRESION si esto cambia.
    sin_rechazo = [
        _cli(0, "como se puede inscribir confirmen"),
        _op(3, "hola, te cuento que somos una plataforma de pronosticos"),
    ]
    r = score_by_motivo(target_messages=sin_rechazo, thread_context="", llm=FakeLLM(_resp()))
    assert r.rating_label == "deficiente"


# --- 2) el numero de 10 digitos que NO es una cedula -------------------------------

def test_un_numero_pedido_como_WHATSAPP_no_es_una_cedula():
    previo = _op(0, "mandame tu whasapp para escribirte mejor")
    assert es_cedula_de_alta("0999367608", previo) is False


def test_un_numero_pedido_como_CEDULA_si_lo_es():
    previo = _op(0, "pasame tu numero de cedula para crearte la cuenta")
    assert es_cedula_de_alta("1501013602", previo) is True


def test_el_CELULAR_pedido_para_el_alta_SI_ancla():
    """Los dos falsos positivos que enseñaron a acotar el patron (`86c8dc60`, `7316b194`).

    El celular ES uno de los tres campos del formulario ("Nombres / Correo electrónico /
    Número de celular"), asi que pedirlo es el ALTA y no un cambio de canal. La primera
    version del guard se los comia: quedaban SIN ANCLA y caian de 4 a 3 estrellas por "no se
    puede medir cuánto tardó".
    """
    for pedido in ("aydame con tu número de celular amigo",
                   "Envíame tu numero de celular para proceder en crearte tu cuenta",
                   "Numero de celular:"):
        assert es_cedula_de_alta("0994993742", _op(0, pedido)) is True, pedido


def test_sin_mensaje_previo_del_operador_se_conserva_el_comportamiento():
    # Falla del lado seguro: sin contexto no se puede afirmar que NO es una cedula.
    assert es_cedula_de_alta("1501013602", None) is True


def test_el_numero_de_contacto_no_ancla_la_ventana():
    """El caso `23049219` reconstruido: el operador pide el WhatsApp al final y ese numero
    se volvia el ancla, corriendo la ventana a un episodio sin respuesta."""
    msgs = [
        _cli(0, "Si ya tengo registrado se puede"),
        _op(1, "a ver mandame tu numero de cedula amigo"),
        _cli(24, "1501055956"),
        _op(31, "amigo, no me sale que tenga usuario"),
        _op(90, "si te animas a crearte la cuenta te dejo mi numero"),
        _op(95, "mandame tu whasapp para escribirte mejor"),
        _cli(100, "0999367608"),
    ]
    r = calificar_registro(msgs)
    assert r is not None
    assert "nadie le respondió" not in r.rationale, f"rationale={r.rationale}"


# --- 3) la sesion entrego credenciales: la ventana no puede desmentirlo -------------

def test_si_la_sesion_entrego_credenciales_no_se_dice_que_nunca_las_recibio():
    """El caso `caa27f9a`: las credenciales salieron en una interaccion anterior y la
    ventana juzgada -la ultima- no las ve. La fila no puede afirmar lo contrario de lo
    que la sesion prueba."""
    msgs = [
        _cli(0, "quiero registrarme"),
        _op(1, "pasame tu correo, celular y nombre de usuario"),
        _cli(5, "vicente@mail.com, 0988942694, vicentenava"),
        _op(40, "Usuario:vicentenava Contraseña: Sorti123."),
        {"created_at": BASE + timedelta(minutes=41), "from_me": True, "is_note": True,
         "body": "Genessis *resuelto* la conversación", "media_type": None},
        _cli(150, "Amigo ayudeme en confirmacion de datos"),
        _cli(151, "1201013602"),
    ]
    # Devuelve None A PROPOSITO: esta ventana no describe el alta, asi que la juzga el
    # camino LLM, que lee la sesion completa. Inventar una nota sobre un episodio que no
    # contiene el alta seria repetir el error con otro numero.
    assert calificar_registro(msgs) is None
