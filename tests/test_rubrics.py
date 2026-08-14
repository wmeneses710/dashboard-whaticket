"""Tests de las rubricas de scoring y del mapeo determinista etiqueta -> estrella.

La calificacion cualitativa (rating_label) la emite el LLM; la estrella es
traduccion determinista de esa etiqueta (tabla que controlamos, NO salida del
modelo). Ver db/scores_schema.sql y src/rubrics.py.
"""
import pytest

from src.rubrics import (
    MOTIVOS,
    RUBRICS,
    derive_aciertos,
    get_rubric,
    label_from_facts,
    label_to_stars,
)


def _facts(atendio=True, extra=False, cortesia=False, maltrato=False,
           claridad="claro", friccion=False, confuso_corroborado=False):
    return label_from_facts(atendio_motivo=atendio, hizo_accion_extra=extra,
                            cortesia_destacada=cortesia, hubo_maltrato_grave=maltrato,
                            claridad=claridad, friccion=friccion,
                            confuso_corroborado=confuso_corroborado)


def test_label_from_facts_maltrato_es_mala():
    assert _facts(atendio=True, maltrato=True) == "mala"
    # maltrato manda aunque haya atendido y con cortesia
    assert _facts(atendio=True, extra=True, cortesia=True, maltrato=True) == "mala"


def test_label_from_facts_no_atendio_es_deficiente():
    assert _facts(atendio=False) == "deficiente"


# ESCALA v4 (definida por el negocio el 2026-08-06):
#   5  se logro el MEJOR ESCENARIO del motivo
#   4  se hizo bien
#   3  falto algo leve
#   2  faltaron varias cosas
#   1  se demoro mucho Y contesto mal, o no contesto
#
# El cambio de fondo respecto de v3: HACER BIEN EL TRABAJO YA VALE 4. Antes el piso
# limpio topaba en 'aceptable' (3) y para pasar de ahi hacia falta el uplift
# COMERCIAL. Medido sobre la tanda del 2026-08-06: en `deposito`, 149 de 213
# sesiones respondieron en <=2 min Y confirmaron la acreditacion, o sea que hicieron
# el trabajo completo, y 135 de esas quedaron en 3. Hacerlo perfecto valia +0,13
# estrellas contra no hacerlo. La escala no medía el comportamiento que decía medir.

def test_hacer_el_trabajo_limpio_YA_ES_buena():
    # El piso limpio es "se hizo bien" = 4. Este es el fin del cap de uplift.
    assert _facts(atendio=True) == "buena"


def test_el_mejor_escenario_es_excelente():
    # Una sola capa por encima del trabajo limpio alcanza para el 5: el uplift dejo
    # de ser un peaje y paso a ser la marca del mejor escenario.
    # ACOTADO el 2026-08-14: la capa tiene que ser una ACCION. La cortesia sola dejo de
    # alcanzar -- era el 46% de los 'excelente' del camino LLM y es casi gratis con
    # plantillas. Ver tests/test_cortesia_no_compra_el_cinco.py.
    assert _facts(atendio=True, extra=True) == "excelente"
    assert _facts(atendio=True, cortesia=True) == "buena"
    assert _facts(atendio=True, extra=True, cortesia=True) == "excelente"


def test_algo_leve_faltando_es_aceptable():
    # El 3 deja de ser el default y pasa a significar lo que dice la escala: se
    # atendio pero quedo algo flojo (confuso sin corroborar).
    assert _facts(atendio=True, claridad="confuso") == "aceptable"


def test_el_empuje_comercial_ya_no_es_peaje_para_pasar_de_3():
    # Sin extra y sin cortesia, atender bien no puede quedar topado en 3.
    assert _facts(atendio=True, extra=False, cortesia=False) != "aceptable"


# --- Modulador v3: claridad + fricción (bajan/limitan la nota desde el piso) -----

def test_confuso_baja_el_piso_a_deficiente():
    # atendió el motivo pero de forma confusa (el cliente tuvo que adivinar) y
    # el confuso esta CORROBORADO por una senal determinista -> 2
    assert _facts(atendio=True, claridad="confuso", confuso_corroborado=True) == "deficiente"


def test_confuso_sin_corroborar_no_hunde_topa_en_aceptable():
    # sin corroboracion determinista, el confuso del LLM NO hunde la nota: topa
    # en el piso (aceptable), no llega a deficiente.
    assert _facts(atendio=True, claridad="confuso", confuso_corroborado=False) == "aceptable"


def test_friccion_baja_el_piso_a_deficiente():
    # atendió pero hubo fricción (cliente reinsistió sin respuesta) -> 2
    assert _facts(atendio=True, friccion=True) == "deficiente"


def test_confuso_bloquea_el_uplift():
    # ni con acción extra + cortesía puede superar deficiente si fue confuso Y
    # esta corroborado
    assert _facts(atendio=True, extra=True, cortesia=True, claridad="confuso",
                  confuso_corroborado=True) == "deficiente"


def test_confuso_sin_corroborar_bloquea_uplift_pero_no_hunde():
    # sin corroboracion, el confuso sigue bloqueando el uplift (no sube a buena/
    # excelente aunque haya extra + cortesía), pero ya no hunde la nota -> aceptable
    assert _facts(atendio=True, extra=True, cortesia=True, claridad="confuso",
                  confuso_corroborado=False) == "aceptable"


def test_dudoso_es_neutral_no_demota_ni_bloquea_uplift():
    # borderline = no-op: no baja el piso (que en v4 es 'buena')...
    assert _facts(atendio=True, claridad="dudoso") == "buena"
    # ...ni impide subir (beneficio de la duda en el eje ambiguo)
    assert _facts(atendio=True, extra=True, cortesia=True, claridad="dudoso") == "excelente"


def test_ghosteo_total_no_atendio_con_friccion_es_mala():
    # NO atendió + fricción (cliente rogando, agente ghosteó) -> 1★ (habilita el extremo)
    assert _facts(atendio=False, friccion=True) == "mala"


def test_no_atendio_sin_friccion_sigue_deficiente():
    # sin fricción, no atender sigue siendo deficiente (no cae a mala)
    assert _facts(atendio=False, friccion=False) == "deficiente"


def test_maltrato_manda_sobre_claridad_y_friccion():
    assert _facts(atendio=True, maltrato=True, claridad="claro", friccion=False) == "mala"


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


# --- derive_aciertos: el "por qué" positivo (espejo de errores[]) ----------

def _aciertos(atendio=True, extra=False, cortesia=False, claridad="claro",
              friccion=False, dimensions=None):
    return derive_aciertos(atendio_motivo=atendio, hizo_accion_extra=extra,
                           cortesia_destacada=cortesia, claridad=claridad,
                           friccion=friccion, dimensions=dimensions)


def _claves(aciertos):
    return [a["clave"] for a in aciertos]


def test_aciertos_piso_limpio_incluye_resolucion_y_claridad():
    assert _claves(_aciertos(atendio=True, claridad="claro")) == ["resolucion", "claridad"]


def test_aciertos_suma_iniciativa_y_cortesia():
    got = _claves(_aciertos(atendio=True, extra=True, cortesia=True, claridad="claro"))
    assert got == ["resolucion", "claridad", "iniciativa", "cortesia"]


def test_aciertos_confuso_no_da_resolucion_ni_claridad():
    assert _aciertos(atendio=True, claridad="confuso") == []


def test_aciertos_friccion_suprime_piso_y_claridad_pero_no_iniciativa():
    # con fricción no se acredita el piso ni "fue claro"; una acción extra real sí sobrevive
    got = _claves(_aciertos(atendio=True, friccion=True, claridad="claro", extra=True))
    assert got == ["iniciativa"]


def test_aciertos_dudoso_da_resolucion_pero_no_claridad():
    # borderline: el piso cuenta como acierto, pero no se afirma "fue claro"
    assert _claves(_aciertos(atendio=True, claridad="dudoso")) == ["resolucion"]


def test_aciertos_no_atendio_es_vacio():
    assert _aciertos(atendio=False) == []


def test_aciertos_usa_la_nota_del_llm_como_evidencia():
    dims = {"resolucion": "Confirmó la recarga con 'ing' tras el comprobante"}
    got = _aciertos(atendio=True, claridad="claro", dimensions=dims)
    res = next(a for a in got if a["clave"] == "resolucion")
    assert res["detalle"] == "Confirmó la recarga con 'ing' tras el comprobante"


# --- formato de esperas para texto que LEE UNA PERSONA -----------------------
# Los rationale de las rubricas se muestran tal cual en el chat y como snippet en la
# lista. Decian cosas como "167s (2.8 min)" — el mismo dato dos veces y con punto
# decimal, que en español se lee mal.

def test_formato_espera_usa_segundos_cuando_es_corto():
    from src.rubrics import formato_espera
    assert formato_espera(42) == "42 segundos"
    assert formato_espera(1) == "1 segundo"


def test_formato_espera_usa_minutos_con_coma_decimal():
    from src.rubrics import formato_espera
    assert formato_espera(167) == "2,8 minutos"
    assert formato_espera(120) == "2 minutos"
    assert formato_espera(60) == "1 minuto"


def test_formato_espera_usa_horas_cuando_es_largo():
    from src.rubrics import formato_espera
    assert formato_espera(7200) == "2 horas"
    assert formato_espera(5400) == "1,5 horas"


def test_formato_espera_sin_dato():
    from src.rubrics import formato_espera
    assert formato_espera(None) == "nunca"


def test_plural_no_escribe_parentesis_ese():
    # "5 pedido(s)" es de programador, no de idioma.
    from src.rubrics import plural
    assert plural(1, "pedido") == "1 pedido"
    assert plural(5, "pedido") == "5 pedidos"


def test_el_plural_mira_el_numero_QUE_SE_MUESTRA():
    # 89s se muestra como "1,5" y eso es plural, aunque 89 < 90. El bug decia
    # "1,5 minuto" porque pluralizaba sobre los segundos crudos.
    from src.rubrics import formato_espera
    assert formato_espera(89) == "1,5 minutos"
    assert formato_espera(62) == "1 minuto"
    assert formato_espera(3700) == "1 hora"
    assert formato_espera(4000) == "1,1 horas"


# --- un ACIERTO no puede contener un reproche -----------------------------------
# Medido el 2026-08-07 con el modelo de prod sobre 45 sesiones: **20 (44,4%)** mostraban
# la critica DENTRO del panel de aciertos. La causa: derive_aciertos usa como evidencia la
# nota de dimension del LLM TEXTUAL, y el modelo la escribe balanceada ("hizo X, pero no
# hizo Y"). El codigo decide bien QUE aciertos hay; el texto que le pega arriba los
# desmiente. Y el 68,9% de las sesiones no producia ningun `errores[]`, asi que la critica
# no desaparecia: se mudaba al unico campo que el front muestra como positivo.
#
# El arreglo de fondo es el contrato del prompt (que la nota diga solo lo que se hizo).
# Este guard es la RED, no el mecanismo: si el modelo desobedece, preferimos la frase por
# defecto antes que presentar un reproche como logro.

def test_un_detalle_con_pero_no_se_muestra_como_acierto():
    aciertos = derive_aciertos(
        atendio_motivo=True, hizo_accion_extra=False, cortesia_destacada=False,
        claridad="claro",
        dimensions={"resolucion": "El operador ofreció crear la cuenta, pero no "
                                  "solicitó los datos necesarios"},
    )
    res = next(a for a in aciertos if a["clave"] == "resolucion")
    assert "pero" not in res["detalle"].lower()
    assert res["detalle"] == "atendio el motivo del cliente"


def test_los_detalles_LIMPIOS_se_conservan():
    # No es censura de palabras: si la nota describe lo hecho, se usa tal cual.
    aciertos = derive_aciertos(
        atendio_motivo=True, hizo_accion_extra=False, cortesia_destacada=False,
        claridad="claro",
        dimensions={"resolucion": "Creó la cuenta y entregó usuario y clave en el chat"},
    )
    res = next(a for a in aciertos if a["clave"] == "resolucion")
    assert res["detalle"] == "Creó la cuenta y entregó usuario y clave en el chat"


@pytest.mark.parametrize("detalle", [
    "Derivó el caso, aunque no ofreció una solución concreta",
    "Explicó el proceso, sin embargo no guio paso a paso",
    "Confirmó el registro pero no completó el proceso",
    "Respondió rápido, faltó cerrar el trámite",
    "Atendió el motivo, no obstante no dio seguimiento",
])
def test_todas_las_marcas_de_contradiccion(detalle):
    aciertos = derive_aciertos(
        atendio_motivo=True, hizo_accion_extra=False, cortesia_destacada=False,
        claridad="claro", dimensions={"resolucion": detalle},
    )
    res = next(a for a in aciertos if a["clave"] == "resolucion")
    assert res["detalle"] == "atendio el motivo del cliente", detalle
