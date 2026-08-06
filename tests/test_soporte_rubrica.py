"""Tests de src/soporte.py: rubrica del motivo `soporte_cuenta`, 100% DETERMINISTA.

EL EJE lo cerro el negocio el 2026-08-05: **velocidad por MEDIANA + el INTENTO**.
Se saca la RESOLUCION a proposito, porque casi siempre ocurre fuera del chat
(desbloqueos, verificaciones, areas tecnicas): calificar el desenlace seria calificar
algo que el operador no controla. Lo que si controla es contestar rapido y hacer algo.

POR QUE LA MEDIANA Y NO EL PEOR TURNO. El peor turno mide CANTIDAD DE TURNOS, no
lentitud: medido el 2026-08-05, retiro con 2,0 turnos daba 63,5% de "peor<=2min" y
soporte con 4,5 turnos daba 36,6%, mientras la mediana se mantenia estable (71-85%)
en los seis motivos. Soporte es justamente el motivo de mas ida y vuelta, asi que el
peor turno lo castigaria por conversar.

ESCALA:
    5  mediana <=2 min + hizo algo concreto + se aseguro de que no faltara nada
    4  mediana <=2 min + hizo algo concreto
    3  mediana <=5 min
    2  mediana >5 min, o no intento nada
    1  no respondio

Umbrales sobre 56 sesiones (1 por persona): la mediana de espera por sesion es 1,1 min
y el 76,8% entra en 2 min. Es el motivo mas rapido de todos.
"""
from datetime import datetime, timedelta, timezone

from src.soporte import calificar_soporte, score_soporte

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

PROBLEMA = "no puedo entrar a mi cuenta, me dice clave incorrecta"
PASO = "Ingresa a la web y toca 'olvide mi clave' para recuperarla"
ESCALO = "Ya escale tu caso al departamento tecnico, te aviso apenas responda"
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


def _cli(minutos, body=PROBLEMA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_con_accion_y_chequeo_de_cierre():
    msgs = [_cli(0), _op(1, PASO), _cli(3, "listo, ya entre"), _op(4, ALGO_MAS)]
    a = calificar_soporte(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_con_accion_pero_sin_chequear():
    msgs = [_cli(0), _op(1, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_escalar_TAMBIEN_es_intentar():
    # La resolucion vive fuera del chat: escalar es lo maximo que puede hacer.
    msgs = [_cli(0), _op(1, ESCALO)]
    assert calificar_soporte(msgs).stars == 4


def test_3_estrellas_si_la_mediana_esta_entre_2_y_5():
    msgs = [_cli(0), _op(4, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_si_la_mediana_pasa_de_5():
    msgs = [_cli(0), _op(9, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_2_estrellas_si_respondio_rapido_pero_NO_intento_nada():
    msgs = [_cli(0), _op(1, "ya lo estamos viendo"), _cli(5, "y?"),
            _op(6, "aguarde")]
    a = calificar_soporte(msgs)
    assert a.stars == 2


def test_1_estrella_si_no_respondio():
    assert calificar_soporte([_cli(0), _cli(4, "hola?")]).stars == 1


# --- la mediana, que es el nucleo del motivo ---------------------------------

def test_UN_turno_lento_no_hunde_una_sesion_por_lo_demas_agil():
    # Cinco turnos: cuatro de 1 min y uno de 20. El peor turno la mandaria a 2; la
    # mediana la deja donde corresponde.
    msgs = [_cli(0), _op(1, PASO),
            _cli(10), _op(11, PASO),
            _cli(20), _op(40, PASO),      # el turno lento
            _cli(50), _op(51, PASO),
            _cli(60), _op(61, PASO)]
    a = calificar_soporte(msgs)
    assert a.stars == 4, f"la mediana deberia mandar, no el peor turno ({a.rationale})"


def test_una_sesion_lenta_de_verdad_SI_baja():
    msgs = [_cli(0), _op(20, PASO), _cli(30), _op(55, PASO), _cli(60), _op(90, PASO)]
    assert calificar_soporte(msgs).stars == 2


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_cli(0), _op(1, PASO),
            _op(2, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_soporte(msgs).stars == 4


def test_el_bot_no_cuenta_como_respuesta():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": PASO, "sent_from": "CHATBOT", "media_type": "chat"}
    assert calificar_soporte([_cli(0), bot]).stars == 1


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": PROBLEMA, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": PASO, "media_type": "chat"}]
    assert calificar_soporte(msgs) is None
    assert score_soporte(msgs) is None


def test_score_soporte_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0), _op(1, PASO), _cli(3, "gracias"), _op(4, ALGO_MAS)]
    r = score_soporte(msgs)
    assert r.motivo == "soporte_cuenta"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_del_4_pide_chequear_el_cierre():
    msgs = [_cli(0), _op(1, PASO)]
    r = score_soporte(msgs)
    assert r.stars == 4 and "algo mas" in r.recomendacion.lower()
