"""Se retiran los dos consejos que el manual de ATC no respalda. Decision del negocio
(2026-08-21), tomada sobre la auditoria de los 40 textos de coaching contra el manual.

COMO SE AUDITO. Se buscaron en el manual (4.609 lineas utiles, acentos y puntuacion
normalizados) las afirmaciones VERIFICABLES de cada texto: umbrales, respuestas rapidas
citadas y prescripciones de conducta.

  CON RESPALDO  el minuto de primera respuesta -- y con su razon: "el sistema lo marca
                automaticamente como leido mediante el doble check azul, por esta razon la
                respuesta del operador debe ser inmediata y no superar un tiempo maximo de
                1 minuto" --, los 5 minutos obligatorios antes de cerrar, los 15 del agente,
                los 30 de continuidad, y /Bienvenida /FIN /R2verificaciondeboleta /R3Recarga.
  CON RESPALDO  el "¿te falta algo más?", que casi se retira por error. La busqueda literal
                da CERO, pero el manual lo pide con otras palabras: "El operador debera
                mantener la conversacion activa hasta confirmar... que el jugador: No
                mantiene dudas pendientes", y el flujo numerado pone "10. Resolucion de
                dudas" antes de "11. Cierre cordial del chat". Son 17.779 recomendaciones:
                borrarlas por una busqueda mal hecha se llevaba la familia mas usada.
  SIN RESPALDO  los dos que este test entierra.

LOS DOS QUE SE VAN

1. `promo._COACHING[4]` -- "Una imagen marcando donde tocar, o un video corto...".
   **6.300 recomendaciones.** El manual prescribe video en SOLO dos procedimientos: el
   tutorial de actualizacion de numero en BackOffice y las "solicitudes de videos
   personalizados" que pide un agente. Para explicar una promo no lo dice en ninguna parte.

   OJO CON LA HISTORIA DE ESTE TEXTO, porque no es una invencion nuestra. El test
   `test_promo_rubrica::test_la_recomendacion_del_4_pide_ALGO_CONCRETO` dejo registrado que
   la version anterior decia "el flyer o el enlace" y **ATC no entendia a que se referia
   (2026-08-11): no usan ninguno de esos dos artefactos**. El vocabulario "imagen o video"
   salio de ellos. Se retira igual, por decision del negocio del 2026-08-21 y ya sabiendo
   esto: no tener respaldo escrito es distinto de estar inventado, y la decision fue que un
   consejo que el manual no sostiene no se emite. Si vuelve, vuelve con la cita.

2. `soporte._COACHING_2_LENTO` -- "Conviene avisar antes de cada consulta interna: 'dejame
   revisar esto y te confirmo en unos minutos'". **373 recomendaciones.** Cero hits en
   cualquier fraseo. Y el manual SI habla de escalamiento, pero todo lo que dice es INTERNO
   (consultar al supervisor antes de escalar, usar el grupo oficial, dejar evidencia): nada
   sobre avisarle al CLIENTE que se esta haciendo una consulta interna.

QUE QUEDA EN SU LUGAR: NADA, y a proposito. La rama devuelve "". No se pone un consejo
generico para tapar el hueco -- un consejo que el negocio nunca pidio es peor que el
silencio, porque el operador lo lee como politica de la empresa. Los huecos quedan medidos
aca (6.300 y 373 recomendaciones) para decidirlos aparte, con el manual en la mano.
"""
from datetime import timedelta

import src.promo
import src.soporte

# Los dos textos, tal como se emitian. Si alguno vuelve al codigo este test lo caza, sin
# importar como vuelva: constante, dict o interpolado.
_VIDEO_PROMO = "Una imagen marcando dónde tocar"
_CONSULTA_INTERNA = "Conviene avisar antes de cada consulta interna"


def _textos_de(mod) -> list[str]:
    """Todo el coaching que la rubrica puede emitir, mire el modulo o el catalogo."""
    from src.catalogo_coaching import CONSEJOS

    nombre = mod.__name__.split(".")[-1]
    out: list[str] = [c.texto for c in CONSEJOS if c.rubrica == nombre]
    for attr in dir(mod):
        if not attr.startswith("_COACHING"):
            continue
        v = getattr(mod, attr)
        if isinstance(v, dict):
            out.extend(str(x) for x in v.values())
        elif isinstance(v, str):
            out.append(v)
    return out


def test_promo_ya_no_prescribe_mandar_un_video():
    for t in _textos_de(src.promo):
        assert _VIDEO_PROMO not in t, \
            "volvio el consejo del video en promo, que el manual no respalda"


def test_soporte_ya_no_prescribe_avisar_la_consulta_interna():
    for t in _textos_de(src.soporte):
        assert _CONSULTA_INTERNA not in t, \
            "volvio el consejo de la consulta interna, que el manual no respalda"


def test_promo_en_cuatro_estrellas_se_queda_SIN_consejo():
    """La rama no se rellena con una frase generica: se queda vacia."""
    from src.catalogo_coaching import consejo_de
    assert consejo_de("promo", "4") is None


def test_promo_no_revienta_al_calificar_cuatro_estrellas():
    """`_COACHING[p.stars]` con `[]` seria un KeyError sobre 6.300 sesiones."""
    from src.promo import score_promo

    from datetime import datetime, timezone
    base = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
    msgs = [
        {"created_at": base, "from_me": False, "is_note": False,
         "body": "¿Cómo reclamo mis 10 giros?", "media_type": "chat"},
        {"created_at": base + timedelta(seconds=30), "from_me": True, "is_note": False,
         "body": "Con solo registrarte, verificar tu cuenta y hacer una recarga desde $5 "
                 "recibes la Freebet de $5 y los 10 giros gratis",
         "media_type": "chat", "sent_from": "OPERATOR"},
    ]
    r = score_promo(msgs)
    assert r is not None
    assert r.stars == 4
    assert r.recomendacion == ""


def test_soporte_dos_estrellas_con_trabajo_no_revienta_ni_inventa():
    """`_coaching` elegia entre SIN_INTENTO y 2_LENTO. Al irse 2_LENTO la rama devuelve ""
    y NO cae en `_COACHING[2]`, que no existe: seria un KeyError sobre 373 sesiones.

    Desde la migracion al catalogo (2026-08-21) la rubrica devuelve la RAMA y el texto sale
    de src/catalogo_coaching.py, asi que la ausencia se expresa como `situacion is None`."""
    s = src.soporte.Soporte(
        stars=2, label="deficiente", rationale="hubo trabajo pero lento",
        mediana=timedelta(minutes=9), intento=True, pregunto_algo_mas=False,
    )
    assert src.soporte._situacion(s) is None


def test_soporte_dos_estrellas_sin_trabajo_conserva_su_consejo():
    """El otro lado de la rama SI tiene respaldo (decirle que sigue y en cuanto tiempo, que
    es el protocolo de seguimiento del manual) y no se toca."""
    s = src.soporte.Soporte(
        stars=2, label="deficiente", rationale="no hizo nada",
        mediana=None, intento=False, pregunto_algo_mas=False,
    )
    from src.catalogo_coaching import consejo_de

    assert src.soporte._situacion(s) == "2_sin_intento"
    assert consejo_de("soporte", "2_sin_intento") is not None


def test_las_demas_ramas_de_promo_siguen_con_su_consejo():
    """La limpieza es quirurgica: los otros tres textos de promo tienen respaldo."""
    from src.catalogo_coaching import consejo_de
    assert consejo_de("promo", "2") is not None
    assert consejo_de("promo", "3") is not None
    assert consejo_de("promo", "1") is not None
