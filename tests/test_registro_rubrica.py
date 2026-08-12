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

from src.registro import (
    calificar_registro,
    es_transaccion,
    interaccion_juzgada,
    score_registro,
)

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


# --- LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL ALTA ------------------------------
# `registro` se quedo afuera del ventaneo por interaccion que `aaadca7` le dio a deposito y
# retiro: `calificar_registro` mira la sesion ENTERA. En las conversaciones con varios
# cierres eso empareja cosas de altas distintas:
#   - `datos` de la interaccion 1 con `cred` de la 5 -> una espera inventada;
#   - y peor, `convirtio` (lo que habilita el 5) agarra una recarga de CUALQUIER
#     interaccion, mientras el texto afirma "en la misma conversacion".
# HALLADO el 2026-08-12 auditando v6: `c4a69129` dice "Creo la cuenta 1,3 minutos despues de
# recibir los datos" y al lado tiene 20.226 minutos (14 dias) de primera respuesta. Y de los
# 13 cinco-estrellas del camino LLM, LOS 13 tienen `cliente_abandono`.

def _cierre(minutos, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def test_no_empareja_los_datos_de_un_alta_con_las_credenciales_de_otra():
    # Interaccion 1: el cliente pasa los datos y nadie le entrega nada -> 2 estrellas.
    # Interaccion 2, tres dias despues: otra alta que si se completa.
    msgs = [_cli(0, DATOS_CLIENTE), _op(3, "ahi te reviso"), _cierre(10),
            _cli(4320, DATOS_CLIENTE), _op(4322, CREDENCIALES), _cierre(4330)]
    r = calificar_registro(msgs)
    assert r is not None
    # Sin ventaneo, `cred` de la 2da tapaba el fracaso de la 1ra y daba 3 o 4.
    assert r.stars == 2, r.rationale


def test_el_5_exige_la_recarga_en_LA_MISMA_interaccion():
    # El alta se completa en la interaccion 1 SIN recarga (=4). La recarga llega en otra
    # interaccion dos dias despues: no puede licenciar el 5 de la primera.
    msgs = [_cli(0, DATOS_CLIENTE), _op(2, CREDENCIALES), _cierre(8),
            _cli(2880, "ahi te mando el comprobante de la recarga"),
            _cli(2881, "", media="image"), _op(2882, "ing"), _cierre(2890)]
    r = calificar_registro(msgs)
    assert r is not None
    assert r.stars == 4, r.rationale


def test_una_sola_interaccion_sigue_dando_lo_mismo():
    # Guard de no-regresion: el 96,3% de las conversaciones tiene UN cierre.
    msgs = [_cli(0, DATOS_CLIENTE), _op(2, CREDENCIALES), _cierre(8)]
    assert calificar_registro(msgs).stars == 4


def test_interaccion_juzgada_expone_la_ventana_del_alta():
    msgs = [_cli(0, DATOS_CLIENTE), _op(2, CREDENCIALES), _cierre(8),
            _cli(4320, DATOS_CLIENTE), _op(4322, CREDENCIALES), _cierre(4330)]
    ventana = interaccion_juzgada(msgs)
    assert ventana is not None
    reales = [m for m in ventana if not m["is_note"]]
    assert reales[0]["created_at"] == BASE          # ancla = el PRIMER traspaso de datos
    assert all(m["created_at"] < BASE + timedelta(minutes=4000) for m in ventana)


def test_interaccion_juzgada_es_None_si_no_hubo_alta():
    assert interaccion_juzgada([_cli(0, "como me registro?"), _op(1, "te explico")]) is None


# `formato_espera(None)` devuelve "nunca", que es correcto en "nunca envio el comprobante"
# pero absurdo incrustado como duracion: "Creo la cuenta NUNCA despues de recibir los datos".
# Pasa cuando las credenciales salen ANTES de que el cliente pase los datos -- el cliente los
# dio por otro canal, 25 de 707 casos, documentado en `es_transaccion`. Ahi `espera` es None.
# MEDIDO el 2026-08-12 en el respaldo v5: **14 filas dicen "nunca despues de recibir los
# datos" y LAS 14 tienen 5 estrellas**, mas 43 que dicen "tardo nunca". Salio a produccion.
# Se corrige el TEXTO, no la nota: cuando no se puede medir la espera, la frase no la afirma.

def test_sin_espera_medible_el_texto_no_dice_nunca():
    # Credenciales primero (datos por otro canal) + recarga -> 5, pero sin duracion medible.
    msgs = [_op(0, CREDENCIALES), _cli(2, DATOS_CLIENTE),
            _cli(3, "ahi va el comprobante de la recarga"), _cli(4, "", media="image"),
            _op(5, "ing")]
    r = calificar_registro(msgs)
    assert r is not None and r.stars == 5
    assert "nunca" not in r.rationale.lower(), r.rationale
    assert r.espera is None


def test_sin_espera_medible_y_sin_recarga_tampoco_dice_nunca():
    msgs = [_op(0, CREDENCIALES), _cli(2, DATOS_CLIENTE)]
    r = calificar_registro(msgs)
    assert r is not None
    assert "nunca" not in r.rationale.lower(), r.rationale


def test_con_espera_medible_el_texto_sigue_diciendo_los_minutos():
    msgs = [_cli(0, DATOS_CLIENTE), _op(2, CREDENCIALES)]
    r = calificar_registro(msgs)
    assert "2 minutos" in r.rationale, r.rationale
