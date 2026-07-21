"""Tests de las rubricas de scoring y del mapeo determinista etiqueta -> estrella.

La calificacion cualitativa (rating_label) la emite el LLM; la estrella es
traduccion determinista de esa etiqueta (tabla que controlamos, NO salida del
modelo). Ver db/scores_schema.sql y src/rubrics.py.
"""
import pytest

from src.rubrics import MOTIVOS, RUBRICS, get_rubric, label_to_stars


def test_rubricas_legacy_human_bot_presentes():
    # human/bot siguen durante la transición (los usan prompts/router hasta el rewire).
    assert {"human", "bot"} <= set(RUBRICS)


def test_rubricas_incluyen_los_siete_motivos():
    assert set(MOTIVOS) == {
        "deposito", "retiro", "soporte_cuenta", "info", "promo", "registro", "problema",
    }
    assert set(MOTIVOS) <= set(RUBRICS)


def test_cada_motivo_tiene_piso_uplift_y_atencion():
    # Modelo de 2 capas: resolucion = PISO (dominant), iniciativa = UPLIFT.
    for m in MOTIVOS:
        spec = get_rubric(m)
        keys = {d.key for d in spec.dimensions}
        assert {"resolucion", "iniciativa", "cortesia"} <= keys
        assert spec.dominant == "resolucion"
        assert spec.uplift == "iniciativa"
        assert spec.label_to_stars["aceptable"] == 3  # piso eficiente


def test_motivos_usan_la_escala_unificada_5_a_1():
    for m in MOTIVOS:
        spec = get_rubric(m)
        assert [spec.label_to_stars[l] for l in spec.labels_desc] == [5, 4, 3, 2, 1]


@pytest.mark.parametrize("rubric,label,stars", [
    ("human", "excelente", 5),
    ("human", "buena", 4),
    ("human", "aceptable", 3),
    ("human", "deficiente", 2),
    ("human", "mala", 1),
    ("bot", "optima", 5),
    ("bot", "funcional", 4),
    ("bot", "mejorable", 3),
    ("bot", "deficiente", 2),
    ("bot", "falla", 1),
])
def test_label_to_stars_es_determinista(rubric, label, stars):
    assert label_to_stars(rubric, label) == stars


def test_cada_rubrica_cubre_1_a_5_de_mejor_a_peor():
    for spec in RUBRICS.values():
        # el mapa cubre exactamente 1..5
        assert sorted(spec.label_to_stars.values()) == [1, 2, 3, 4, 5]
        # labels_desc va de la mejor (5) a la peor (1)
        assert [spec.label_to_stars[l] for l in spec.labels_desc] == [5, 4, 3, 2, 1]


def test_la_dimension_dominante_existe_en_la_rubrica():
    for spec in RUBRICS.values():
        keys = {d.key for d in spec.dimensions}
        assert spec.dominant in keys


def test_rubrica_desconocida_falla():
    with pytest.raises(ValueError):
        get_rubric("robot")


def test_etiqueta_de_otra_rubrica_falla():
    # "optima" es una etiqueta de bot, no de human.
    with pytest.raises(ValueError):
        label_to_stars("human", "optima")
