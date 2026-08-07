"""Tests del mapeo cola -> segmento.

Los nombres de cola salen de la data real capturada por el ETL
(output/{sistemas,datos}/whaticket_audit-*.json). Se testean tal cual
aparecen, incluyendo el emoji de "Agente" y el typo "Makerting".
"""
import pytest

from src.segments import (
    ALL_SEGMENTS,
    AMBIENTES,
    ambiente_incluye_sin_cola,
    segment_for_queue,
    segments_for_ambiente,
)


@pytest.mark.parametrize("queue_name", [
    "Jugadores",
    "OnlySorti",
    "sortiGO",
    "ModoSorti",
])
def test_colas_de_jugador(queue_name):
    assert segment_for_queue(queue_name) == "jugador"


def test_cola_de_agente_con_emoji():
    assert segment_for_queue("Agente \U0001F468\U0001F469") == "agente"


def test_cola_de_marketing_con_typo_real():
    # En la data el nombre viene mal escrito: "Makerting".
    assert segment_for_queue("Departamento de Makerting") == "marketing"


@pytest.mark.parametrize("queue_name", ["", "   ", None])
def test_cola_vacia_es_interno(queue_name):
    assert segment_for_queue(queue_name) == "interno"


def test_cola_prueba_se_descarta():
    assert segment_for_queue("Prueba") == "descartar"


@pytest.mark.parametrize("queue_name,esperado", [
    ("  jugadores  ", "jugador"),       # espacios
    ("ONLY SORTI", "jugador"),          # mayúsculas y espacio
    ("onlysorti", "jugador"),           # todo minúscula
    ("modo sorti", "jugador"),          # con espacio
    ("AGENTE", "agente"),               # sin emoji
])
def test_normalizacion_robusta(queue_name, esperado):
    assert segment_for_queue(queue_name) == esperado


def test_cola_desconocida_cae_en_otro():
    assert segment_for_queue("Cola nueva rarísima") == "otro"


# --- AMBIENTES: el agrupador GRUESO del dashboard -------------------------------
# Jerarquia de filtros definida por el negocio (2026-08-07): manda OPERADORES
# (activos/apagados), y adentro de eso el AMBIENTE. Cuatro: todos, jugador, agente y
# sin_clasificar. El cuarto existe porque medido sobre la copia de prod hay 17.780
# sesiones que no son ninguna de las dos audiencias de negocio (16.910 de ellas con
# `queue_id IS NULL`) y arrastran 6.932 comprobantes de deposito. Fundirlas en `agente`
# le atribuiria esos depositos a los agentes, que es justo lo que el switch viene a evitar.

def test_los_cuatro_ambientes():
    assert AMBIENTES == ("todos", "jugador", "agente", "sin_clasificar")


def test_jugador_y_agente_son_un_solo_segmento_cada_uno():
    assert segments_for_ambiente("jugador") == ("jugador",)
    assert segments_for_ambiente("agente") == ("agente",)


def test_sin_clasificar_junta_lo_que_no_es_audiencia_de_negocio():
    assert set(segments_for_ambiente("sin_clasificar")) == {
        "interno", "marketing", "otro", "descartar"}


def test_todos_es_LITERALMENTE_todo():
    # Sin exclusiones escondidas: 'todos' tiene que dar la union exacta de los otros
    # tres. Si alguien agrega un segmento y se olvida de asignarlo a un ambiente, las
    # filas desaparecerian en silencio de los cuatro tableros; este test lo impide.
    union = set(segments_for_ambiente("jugador")) | set(segments_for_ambiente("agente")) \
        | set(segments_for_ambiente("sin_clasificar"))
    assert set(segments_for_ambiente("todos")) == union == set(ALL_SEGMENTS)


def test_los_ambientes_PARTICIONAN_los_segmentos():
    # Ningun segmento en dos ambientes a la vez (si no, se contaria doble en 'todos').
    vistos = []
    for amb in ("jugador", "agente", "sin_clasificar"):
        vistos.extend(segments_for_ambiente(amb))
    assert len(vistos) == len(set(vistos)) == len(ALL_SEGMENTS)


def test_todo_lo_que_devuelve_segment_for_queue_esta_en_algun_ambiente():
    # El contrato de verdad: cualquier salida posible de la clasificacion tiene ambiente.
    for cola in ("Jugadores", "Agente 👨👩", "Departamento de Makerting", None,
                 "Prueba", "Cola nueva rarisima"):
        assert segment_for_queue(cola) in segments_for_ambiente("todos")


def test_ambiente_desconocido_falla_fuerte():
    # No se degrada a 'todos' en silencio: un typo en el query param que abriera el
    # tablero entero seria peor que un error.
    with pytest.raises(ValueError):
        segments_for_ambiente("jugadores")


def test_solo_los_ambientes_con_interno_incluyen_las_conversaciones_SIN_COLA():
    # `queue_id IS NULL` -> segment_for_queue(None) == 'interno'. Los cuadros filtran por
    # queue_id, asi que ESTA es la señal de si hay que sumarle el `OR queue_id IS NULL`.
    assert ambiente_incluye_sin_cola("sin_clasificar") is True
    assert ambiente_incluye_sin_cola("todos") is True
    assert ambiente_incluye_sin_cola("jugador") is False
    assert ambiente_incluye_sin_cola("agente") is False
