"""Que cuadros APLICAN a cada ambiente, y que se declare cuando no.

AUDITORIA del 2026-08-07 sobre las 25 queries del tablero:
  - 12 respetan el ambiente via `_scores_filters` (conversation_scores)   -> OK
  - 5 lo respetan via queue_ids (los 3 cuadros full-scale, pendientes, composicion) -> OK
  - 4 lo IGNORABAN y devolvian SIEMPRE datos de jugador: las de conversion.
  - `filter_options` ofrecia valores que en el ambiente activo no existen.

EL CASO PELIGROSO. `_conversion_where` recibe `**_ignored` y se tragaba el `ambiente`, y
`player_conversions` guarda `'jugador'` HARDCODEADO (src/conversions.py). Verificado en vivo:
`/api/conversion` devolvia el MISMO hash md5 para jugador, agente y sin_clasificar. O sea
que apretabas "Agentes" y las tarjetas de conversion seguian mostrando jugadores, sin ningun
aviso. Peor que una tarjeta vacia: una tarjeta que miente.

DECISION: la conversion es, por definicion, "jugador potencial -> jugador". No es un filtro
que falte: es una metrica que solo existe para jugadores. Se DECLARA en la respuesta en vez
de fingir que aplica.
"""
from src.queries import conversion_aplica


def test_la_conversion_solo_aplica_a_jugadores():
    assert conversion_aplica("jugador") is True


def test_no_aplica_a_los_otros_ambientes():
    for amb in ("agente", "sin_clasificar"):
        assert conversion_aplica(amb) is False, amb


def test_en_todos_aplica_porque_el_total_incluye_jugadores():
    # 'todos' no es una audiencia: es la suma. La conversion de los jugadores que hay
    # adentro sigue siendo un dato valido, y esconderla seria peor.
    assert conversion_aplica("todos") is True
