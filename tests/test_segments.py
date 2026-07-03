"""Tests del mapeo cola -> segmento.

Los nombres de cola salen de la data real capturada por el ETL
(output/{sistemas,datos}/whaticket_audit-*.json). Se testean tal cual
aparecen, incluyendo el emoji de "Agente" y el typo "Makerting".
"""
import pytest

from src.segments import segment_for_queue


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
