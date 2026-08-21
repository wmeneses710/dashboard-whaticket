"""El reloj de `info` arrancaba en un "Gracias".

`calificar_info` ancla en el PRIMER mensaje del cliente, sea lo que sea. Cuando ese primer
mensaje es un saludo o un agradecimiento, la espera que se le cobra al operador incluye un
tramo en el que no habia nada que contestar.

Su propio docstring ya tenia el criterio correcto -- "hubo algo que responder", el
complemento de `sin_motivo` -- y `es_cortesia` existe y ya se usa en `src/agilidad.py`
(confound 2: "el peor turno de una sesion suele ser un 'Ok' o un 'Gracias'"). Lo que
faltaba era aplicarlo al ANCLA.

MEDIDO el 2026-08-17 sobre la corrida v16 completa: **379 de las 2.033 sesiones de `info`
(18,6%)** abren con una cortesia del cliente y traen el planteo real despues. Al medir desde
el planteo, **92 cambian de banda: 58 suben y 34 bajan**. NO ES INDULGENCIA: el intervalo
estaba mal medido en las dos direcciones, y hay operadores que hoy salen mejor de lo que
les corresponde porque contestaron rapido el saludo y tarde la consulta.

Caso `58a51842` (Anya Alexandra, 2 estrellas): el cliente dice "Gracias", seis minutos
despues pregunta "Como Aser apuestas" y la operadora manda el video tutorial **al minuto
siguiente**. La nota decia "Respondió recién 6,5 minutos después de la consulta".

SI TODO LO DEL CLIENTE ES CORTESIA el ancla no se mueve: la sesion se sigue midiendo como
hasta ahora. Cambiar eso mandaria la fila al camino con LLM (score_info devolviendo None),
que es una decision de routing y no de reloj.
"""
from datetime import datetime, timedelta, timezone

from src.info import calificar_info

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

CONSULTA = "Como Aser apuestas"
RESPUESTA = "Te comparto el siguiente video tutorial para que puedas realizar tu primera apuesta"


def _cli(minutos, body=CONSULTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body=RESPUESTA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


def test_el_gracias_de_entrada_no_arranca_el_reloj():
    """El caso 58a51842: contesto la consulta en 1 minuto, no en 6,5."""
    i = calificar_info([
        _cli(0, "Gracias"),
        _cli(6, CONSULTA),
        _op(7, RESPUESTA),
    ])
    assert i is not None
    assert i.espera == timedelta(minutes=1)
    assert i.stars == 4


def test_el_saludo_de_entrada_tampoco():
    i = calificar_info([
        _cli(0, "Hola buenas tardes"),
        _cli(10, "¿cuales son los horarios de atencion?"),
        _op(11, "Atendemos de 6 de la mañana a medianoche"),
    ])
    assert i is not None
    assert i.espera == timedelta(minutes=1)


def test_contestar_rapido_el_saludo_no_tapa_la_consulta_lenta():
    """La otra direccion, y por eso esto es correccion y no indulgencia: 34 de las 92
    filas que cambian BAJAN. El reloj tiene que medir la consulta, no el saludo."""
    i = calificar_info([
        _cli(0, "Hola"),
        _op(1, "Buenas tardes 😉"),
        _cli(2, CONSULTA),
        _op(20, RESPUESTA),
    ])
    assert i is not None
    assert i.espera == timedelta(minutes=18)
    assert i.stars == 2


def test_si_el_primer_mensaje_ya_es_el_planteo_nada_cambia():
    i = calificar_info([_cli(0, CONSULTA), _op(3, RESPUESTA)])
    assert i is not None
    assert i.espera == timedelta(minutes=3)
    assert i.stars == 3


def test_si_todo_es_cortesia_el_ancla_no_se_mueve():
    """Sin planteo no hay a donde mover el ancla: se mide como siempre y la fila NO se va
    al camino con LLM."""
    i = calificar_info([_cli(0, "Gracias"), _cli(1, "Ok"), _op(9, "Un placer 🍀")])
    assert i is not None
    assert i.espera == timedelta(minutes=9)
    assert i.stars == 2


def test_el_ancla_no_se_mueve_a_un_mensaje_que_nadie_contesto():
    """EL GUARD QUE COSTO 5 FALSOS 1 ESTRELLA (medido el 2026-08-17 antes de subirlo).

    Mover el ancla a un mensaje FINAL sin respuesta manda la fila a la rama de 1 estrella
    ("El cliente preguntó y nadie le respondió"), que es la nota mas cara del sistema. Y el
    vocabulario de cortesia es CERRADO a proposito, asi que cualquier palabra que no este en
    la lista se vuelve un "planteo": `0ccb648c` cerraba con "super", `6d6f093b` con "Que dios
    los vendiga", `0ebb1ecf` con "nada, tranqui". Ninguno es una consulta.

    Regla: si al planteo elegido no le sigue NINGUNA respuesta del operador, el ancla vuelve
    al primer mensaje del cliente. Este arreglo saca notas falsas, no agrega notas nuevas.
    """
    i = calificar_info([
        _cli(0, "excelente"),
        _op(0, "Bueno"),
        _cli(72, "super"),
    ])
    assert i is not None
    assert i.stars == 4
    assert "nadie le respondió" not in i.rationale


def test_el_adjunto_del_cliente_es_planteo_aunque_el_texto_sea_cortesia():
    """Mismo criterio que `client_sin_motivo`: mandar algo es plantear algo."""
    i = calificar_info([
        _cli(0, "Hola"),
        _cli(4, "gracias", media="image"),
        _op(5, "Ya lo reviso"),
    ])
    assert i is not None
    assert i.espera == timedelta(minutes=1)
