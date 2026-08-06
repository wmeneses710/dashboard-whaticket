"""Tests de src/registro.py: rubrica del motivo `registro`, 100% DETERMINISTA.

Todo PURO, en memoria, sin LLM y sin BD.

LA DEFINICION la cerro el negocio el 2026-08-06: `registro` es UNA sola cosa — el
cliente pasa sus datos y el operador le devuelve las credenciales. Eso convierte un
cliente potencial en jugador. Y si ademas logro que depositara, es el mejor escenario
posible: "5 de ley, sin importar el orden" (el deposito antes de las credenciales
cuenta igual; son 3 de 108 casos y el negocio decidio no perseguirlos).

ESCALA:
    5  entrego credenciales Y logro el deposito en la misma sesion
    4  entrego credenciales dentro de los 5 min del traspaso de datos
    3  entrego credenciales pero tardo mas de 5 min
    2  el cliente paso sus datos y NUNCA recibio credenciales (alta a medias)
    1  el cliente pidio registrarse y no hubo respuesta

UMBRAL, calibrado sobre 707 registros (1 sesion por persona, jul-ago 2026): del
traspaso de datos a las credenciales la mediana es 3,1 min y el 69,1% entra en 5 min.
El corte de 2 min que usan deposito y retiro aca seria injusto: solo el 26,3% lo
alcanza, porque crear una cuenta lleva mas que acusar un comprobante.
"""
from datetime import datetime, timedelta, timezone

from src.registro import calificar_registro, es_transaccion, score_registro

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

DATOS_CLIENTE = "Nancy Toaquiza toaquizanancy68@gmail.com 0986987466"
CREDENCIALES = "Estas son tus credenciales Usuario: nancy593 Clave: 12345"
PIDE_DATOS = "Ayudame con los datos para tu registro Nombre de usuario: Correo:"


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- el corte transaccion / consulta ----------------------------------------

def test_preguntar_como_registrarse_no_es_transaccion():
    # Sin datos del cliente ni credenciales entregadas no hubo alta: es una consulta.
    msgs = [_cli(0, "hola, como me registro?"),
            _op(1, "te ayudo, es facil, entras a la web")]
    assert es_transaccion(msgs) is False
    assert calificar_registro(msgs) is None


def test_el_traspaso_de_datos_ya_hace_transaccion():
    # Aunque no haya credenciales: el alta ARRANCO y quedo a medias. Eso se califica.
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE)]
    assert es_transaccion(msgs) is True


def test_las_credenciales_solas_tambien_hacen_transaccion():
    # El cliente pudo pasar los datos por otro canal (25 de 707 casos medidos).
    assert es_transaccion([_cli(0, "quiero jugar"), _op(2, CREDENCIALES)]) is True


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_credenciales_MAS_deposito():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(3, CREDENCIALES),
            _cli(10, "listo, ahi va mi recarga"), _cli(10, "", media="image")]
    a = calificar_registro(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_5_estrellas_aunque_el_deposito_venga_ANTES():
    # Decision del negocio: "cuenta por un tema estadistico, algo de suerte es pero
    # asi queda". El orden es un detalle operativo.
    msgs = [_cli(0, "ya hice la recarga"), _cli(0, "", media="image"),
            _op(1, PIDE_DATOS), _cli(2, DATOS_CLIENTE), _op(4, CREDENCIALES)]
    assert calificar_registro(msgs).stars == 5


def test_4_estrellas_credenciales_dentro_de_5_min():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(4, CREDENCIALES)]
    a = calificar_registro(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_credenciales_pero_lentas():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(20, CREDENCIALES)]
    a = calificar_registro(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_el_cliente_paso_datos_y_nunca_hubo_credenciales():
    # El alta a medias: lo peor que puede pasarle a un cliente potencial.
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE),
            _op(5, "dame un momento"), _op(30, "seguimos mañana")]
    a = calificar_registro(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_1_estrella_paso_los_datos_y_nadie_contesto():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE)]
    a = calificar_registro(msgs)
    assert a.stars == 1 and a.label == "mala"


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(3, CREDENCIALES),
            _op(4, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_registro(msgs).stars == 4


def test_el_reloj_arranca_en_el_TRASPASO_no_en_el_saludo():
    msgs = [_cli(0, "buenas"), _op(1, "hola!"), _op(40, PIDE_DATOS),
            _cli(41, DATOS_CLIENTE), _op(43, CREDENCIALES)]
    assert calificar_registro(msgs).stars == 4


def test_pedir_credenciales_no_es_entregarlas():
    # `operator_sent_credentials` distingue entregar de PEDIR; se fija el contrato.
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE),
            _op(2, "pasame tu usuario: y tu clave: para verificar")]
    assert calificar_registro(msgs).stars in (1, 2)


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": DATOS_CLIENTE, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": CREDENCIALES, "media_type": "chat"}]
    assert calificar_registro(msgs) is None
    assert score_registro(msgs) is None


def test_score_registro_devuelve_un_ScoreResult_usable():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(3, CREDENCIALES),
            _cli(10, "ahi va mi recarga"), _cli(10, "", media="image")]
    r = score_registro(msgs)
    assert r.motivo == "registro"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_del_4_apunta_al_deposito():
    msgs = [_op(0, PIDE_DATOS), _cli(1, DATOS_CLIENTE), _op(3, CREDENCIALES)]
    r = score_registro(msgs)
    assert r.stars == 4
    assert "deposit" in r.recomendacion.lower() or "recarga" in r.recomendacion.lower()
