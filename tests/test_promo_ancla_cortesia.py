"""El mismo reloj arrancando en un "Hola", ahora en `promo`.

`calificar_promo` anclaba en el primer mensaje del cliente igual que `calificar_info`, y la
poblacion es cinco veces mas grande: **658 de las 10.163 sesiones deterministas (6,5%) abren
con una cortesia y traen el planteo despues** (medido el 2026-08-17 sobre la corrida v16).

El caso que lo muestra entero es `07b642b4`: la nota decia 8,6 HORAS de espera y el operador
contesto la consulta real **6 segundos** despues de que llegara.

Va con el MISMO guard que `info` (ver `signals.planteo_del_cliente`): si al planteo no le
sigue ninguna respuesta, el ancla no se mueve. Sin eso el arreglo fabrica notas de 1 estrella
("El cliente preguntó por la promo y nadie le respondió") sobre despedidas.
"""
from datetime import datetime, timedelta, timezone

from src.promo import calificar_promo

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

CONSULTA = "¿Cómo reclamo mis 10 giros?"
RESPUESTA = ("Con solo registrarte, verificar tu cuenta y hacer una recarga desde $5 "
             "recibes la Freebet de $5 y los 10 giros gratis")


def _cli(minutos, body=CONSULTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body=RESPUESTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


def test_el_saludo_de_entrada_no_arranca_el_reloj():
    """La forma de `07b642b4`: el reloj contaba desde el saludo, no desde la consulta."""
    p = calificar_promo([
        _cli(0, "Hola"),
        _cli(120, CONSULTA),
        _op(121, RESPUESTA),
    ])
    assert p is not None
    assert p.espera == timedelta(minutes=1)
    assert p.stars == 4
    assert "Respondió recién" not in p.rationale


def test_contestar_rapido_el_saludo_no_tapa_la_consulta_lenta():
    """La otra direccion: 20 de las filas que cambian BAJAN."""
    p = calificar_promo([
        _cli(0, "Buenas"),
        _op(1, "Hola, un placer tenerte por aqui"),
        _cli(2, CONSULTA),
        _op(30, RESPUESTA),
    ])
    assert p is not None
    assert p.espera == timedelta(minutes=28)
    assert p.stars == 2


def test_el_ancla_no_se_mueve_a_un_mensaje_que_nadie_contesto():
    """El guard: sin el, una despedida se vuelve "nadie le respondió"."""
    p = calificar_promo([
        _cli(0, CONSULTA),
        _op(1, RESPUESTA),
        _cli(90, "listo bro, muchas gracias por todo"),
    ])
    assert p is not None
    assert p.stars >= 4
    assert "nadie le respondió" not in p.rationale


def test_si_el_primer_mensaje_ya_es_el_planteo_nada_cambia():
    p = calificar_promo([_cli(0, CONSULTA), _op(3, RESPUESTA)])
    assert p is not None
    assert p.espera == timedelta(minutes=3)
    assert p.stars == 3


def test_el_material_sigue_mandando_sobre_el_reloj():
    """El eje de la rubrica no se toca: con captura y espera razonable, 5 estrellas."""
    p = calificar_promo([
        _cli(0, "Hola"),
        _cli(10, CONSULTA),
        _op(11, RESPUESTA),
        _op(11, "", media="image"),
    ])
    assert p is not None
    assert p.stars == 5
