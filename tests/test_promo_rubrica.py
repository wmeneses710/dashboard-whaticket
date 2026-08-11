"""Tests de src/promo.py: rubrica del motivo `promo`, 100% DETERMINISTA.

`promo` es el UNICO motivo donde el eje de uplift sobrevive, y esta probado con
datos de negocio (2026-08-05): con empuje + MATERIAL el deposito posterior sube de
24,9% a 34,1%, mientras que empujar SOLO CON PALABRAS da 19,1% — peor que no hacer
nada. Por eso el material no es un adorno: es la condicion.

ESCALA:
    5  mando MATERIAL y respondio <=5 min   (el mejor escenario probado)
    4  respondio <=2 min, sin material
    3  respondio entre 2 y 15 min, sin material
    2  respondio despues de 15 min
    1  no respondio

POR QUE EL MATERIAL ES PALANCA DEL 5 Y NO DEL 4. Medido sobre 424 sesiones (1 por
persona): solo el 11,8% manda material. Si fuera el corte del 4, el 88% quedaria
debajo y volveriamos a una escala aplastada. Como palanca del 5 queda un techo
exigente pero alcanzable (11,6%), que es justo lo que el negocio quiere empujar.
"""
from datetime import datetime, timedelta, timezone

from src.promo import calificar_promo, score_promo

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

PREGUNTA = "¿Como reclamo mis 10 giros?"
EXPLICA = "Te cuento: con tu primera recarga se activan los 10 giros gratis."
EMPUJE = "Registrate aca https://www.sorti.ec/register y aprovecha la promo"


def _cli(minutos, body=PREGUNTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


def _flyer(minutos):
    return _op(minutos, body="mira la promo", media="image")


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_con_material_a_tiempo():
    msgs = [_cli(0), _op(1, EXPLICA), _flyer(1), _op(2, EMPUJE)]
    a = calificar_promo(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_el_link_tambien_cuenta_como_material():
    # Un enlace a la promo es material: el cliente se lleva algo concreto.
    msgs = [_cli(0), _op(1, EXPLICA + " " + EMPUJE)]
    assert calificar_promo(msgs).stars == 5


def test_4_estrellas_rapido_pero_sin_material():
    msgs = [_cli(0), _op(1, EXPLICA)]
    a = calificar_promo(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_si_tardo_entre_2_y_15():
    msgs = [_cli(0), _op(7, EXPLICA)]
    a = calificar_promo(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_si_tardo_mas_de_15():
    msgs = [_cli(0), _op(40, EXPLICA)]
    a = calificar_promo(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_1_estrella_si_no_respondio():
    msgs = [_cli(0), _cli(3, "hola?")]
    a = calificar_promo(msgs)
    assert a.stars == 1 and a.label == "mala"


# --- el nucleo del motivo ----------------------------------------------------

def test_EMPUJAR_SIN_MATERIAL_no_llega_al_5():
    # La conducta mayoritaria (~56% de las sesiones) y la que el negocio midio como
    # CONTRAPRODUCENTE: 19,1% de conversion contra 24,9% de no hacer nada.
    msgs = [_cli(0), _op(1, "aprovecha la promo, animate y me avisas")]
    assert calificar_promo(msgs).stars == 4, "sin material no puede ser el mejor escenario"


def test_el_material_vale_mas_que_un_par_de_minutos():
    # Flyer a los 4 min (5) le gana a explicar de palabra en 1 min (4).
    assert calificar_promo([_cli(0), _op(4, EXPLICA), _flyer(4)]).stars == 5
    assert calificar_promo([_cli(0), _op(1, EXPLICA)]).stars == 4


def test_material_tarde_ya_no_salva():
    assert calificar_promo([_cli(0), _op(9, EXPLICA), _flyer(9)]).stars == 3


def test_la_cortesia_sola_NO_alcanza_para_el_5():
    msgs = [_cli(0), _op(1, EXPLICA),
            _op(2, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    assert calificar_promo(msgs).stars == 4


def test_el_bot_no_cuenta_como_respuesta():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": EXPLICA, "sent_from": "CHATBOT",
           "media_type": "chat"}
    assert calificar_promo([_cli(0), bot]).stars == 1


def test_sin_created_at_cede_el_turno():
    msgs = [{"from_me": False, "is_note": False, "body": PREGUNTA, "media_type": "chat"},
            {"from_me": True, "is_note": False, "body": EXPLICA, "media_type": "chat"}]
    assert calificar_promo(msgs) is None
    assert score_promo(msgs) is None


def test_score_promo_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0), _op(1, EXPLICA), _flyer(1), _op(2, EMPUJE)]
    r = score_promo(msgs)
    assert r.motivo == "promo"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == ""


def test_la_recomendacion_del_4_pide_ALGO_CONCRETO():
    # El consejo tiene que nombrar QUE mandar en el idioma del equipo. Decia "falta el
    # material ... el flyer o el enlace" y atencion al cliente no entendia a que se referia
    # (2026-08-11): no usan ninguno de esos dos artefactos. Ver
    # tests/test_vocabulario_retroalimentacion.py.
    msgs = [_cli(0), _op(1, EXPLICA)]
    r = score_promo(msgs)
    assert r.stars == 4
    consejo = r.recomendacion.lower()
    assert "flyer" not in consejo
    assert any(p in consejo for p in ("imagen", "video")), consejo
