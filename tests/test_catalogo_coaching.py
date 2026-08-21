"""El coaching determinista pasa a ser un CATALOGO CERRADO con codigo.

POR QUE. El coaching de hoy se cuenta igual que se contaban los errores antes de v21: no se
puede. MEDIDO el 2026-08-21 sobre la copia, 79.918 recomendaciones en 124.341 filas
evaluadas, y el corte por autor parte el problema en dos mitades muy distintas:

    determinista/agilidad-v1    22.076 recomendaciones ->      4 textos distintos
    determinista/deposito-v1    14.823                 ->     22
    determinista/promo-v1       12.165                 ->     16
    determinista/registro-v1     7.552                 ->     16
    determinista/soporte-v1      4.700                 ->     16
    determinista/info-v1         4.680                 ->     13
    determinista/retiro-v1       1.759                 ->     11
                                ------                     ----
                                67.755                 ->     98
    qwen3:14b                   12.163                 -> 10.325   (84,9% unicos)

Las siete rubricas deterministas YA SON un catalogo cerrado -- 98 textos para 67.755
recomendaciones -- solo que no esta declarado como tal, asi que el tablero no puede decir
"esta semana 40 operadores recibieron este consejo". Declararlo es todo lo que falta.
(Los 98 son ~38 textos base por las combinaciones del apendice de `refine_recomendacion`;
el codigo identifica la BASE y los fragmentos llevan el suyo.)

LA REGLA DE ESTE ARCHIVO ES LA DE `catalogo_atc.py`: **el campo `texto` es el que ya se
venia emitiendo, VERBATIM.** Este cambio no reescribe coaching, solo le pone nombre a lo que
ya se emite. Si algun texto hay que mejorarlo, es otro cambio y se mide aparte.

Y CADA CONSEJO SE ATA A UNA BUENA PRACTICA DEL MANUAL (B01-B12). Es lo que vuelve el
coaching auditable de punta a punta: el error dice E0x, el consejo dice "para cumplir B10".
Sin ese enganche seguimos hablando nuestro idioma y no el de ellos.

PRIMERA TAJADA: `agilidad`. Cuatro textos que cubren 22.076 recomendaciones (el 28% de todo
el coaching del sistema) y es el segmento que ademas no tiene motivos, donde cae el ladrillo
siguiente. Las otras seis rubricas entran igual, mecanicamente.
"""
from src.catalogo_atc import CODIGOS_PRACTICA
from src.catalogo_coaching import (
    CONSEJO_POR_CODIGO,
    CONSEJOS,
    FRAGMENTOS,
    consejo_de,
)


# --- el catalogo se sostiene solo ------------------------------------------------------
def test_los_codigos_son_unicos():
    codigos = [c.codigo for c in CONSEJOS]
    assert len(codigos) == len(set(codigos))


def test_el_indice_cubre_todo_el_catalogo():
    assert set(CONSEJO_POR_CODIGO) == {c.codigo for c in CONSEJOS}


def test_ningun_texto_esta_vacio():
    for c in CONSEJOS:
        assert c.texto.strip(), f"{c.codigo} sin texto"


def test_cada_consejo_apunta_a_una_practica_del_manual():
    """El enganche con B01-B12 es lo que lo hace auditable en el idioma de ATC."""
    for c in CONSEJOS:
        assert c.practica in CODIGOS_PRACTICA, \
            f"{c.codigo} apunta a una practica que no existe: {c.practica!r}"


def test_los_fragmentos_tambien_tienen_codigo():
    """`refine_recomendacion` agrega texto al consejo base; si no lleva codigo, la fila
    vuelve a ser incontable justo cuando el apendice es lo que se leyo."""
    assert FRAGMENTOS, "los fragmentos deterministas tienen que estar en el catalogo"
    for f in FRAGMENTOS:
        assert f.codigo and f.texto.strip()
    codigos = [f.codigo for f in FRAGMENTOS]
    assert len(codigos) == len(set(codigos))


# --- agilidad, la primera rubrica ------------------------------------------------------
def test_agilidad_tiene_consejo_para_las_cuatro_etiquetas_que_lo_llevan():
    for etiqueta in ("mala", "deficiente", "aceptable", "buena"):
        c = consejo_de("agilidad", etiqueta)
        assert c is not None, f"agilidad/{etiqueta} sin consejo"


def test_excelente_no_lleva_consejo():
    """Cinco estrellas no tiene nada que mejorar. Devolver un consejo ahi seria inventar
    una falta para poder mostrar algo."""
    assert consejo_de("agilidad", "excelente") is None


def test_una_situacion_desconocida_no_rompe():
    assert consejo_de("agilidad", "loquesea") is None
    assert consejo_de("rubrica-que-no-existe", "buena") is None


# --- el contrato con la rubrica: no puede haber dos fuentes de verdad ------------------
def test_score_agilidad_emite_el_texto_del_catalogo():
    """Si la rubrica se guarda su copia del texto, el catalogo miente en cuanto uno de los
    dos cambie. El texto emitido tiene que SER el del catalogo, no parecerse."""
    from datetime import datetime, timedelta, timezone

    from src.agilidad import score_agilidad

    base = datetime(2026, 3, 10, 14, 0, 0, tzinfo=timezone.utc)
    # Un pedido del agente que el operador contesta 7 minutos despues: cae en 'aceptable'
    # (mas de 5 min, menos de 15) segun los umbrales de la rubrica.
    msgs = [
        {"created_at": base, "from_me": False, "is_note": False,
         "body": "cargame 50 al usuario juan01", "sent_from": None, "user_id": None,
         "media_type": "chat", "ack": 3},
        {"created_at": base + timedelta(minutes=7), "from_me": True, "is_note": False,
         "body": "listo, ya quedo acreditado", "sent_from": "WEB", "user_id": "op1",
         "media_type": "chat", "ack": 3},
    ]
    score = score_agilidad(msgs)
    assert score is not None
    consejo = consejo_de("agilidad", score.rating_label)
    if consejo is None:
        assert score.recomendacion == ""
    else:
        assert score.recomendacion == consejo.texto


def test_score_agilidad_persiste_el_codigo():
    """Sin el codigo en la fila no se puede contar, que es todo el punto del cambio."""
    from datetime import datetime, timedelta, timezone

    from src.agilidad import score_agilidad

    base = datetime(2026, 3, 10, 14, 0, 0, tzinfo=timezone.utc)
    msgs = [
        {"created_at": base, "from_me": False, "is_note": False,
         "body": "cargame 50 al usuario juan01", "sent_from": None, "user_id": None,
         "media_type": "chat", "ack": 3},
        {"created_at": base + timedelta(minutes=7), "from_me": True, "is_note": False,
         "body": "listo, ya quedo acreditado", "sent_from": "WEB", "user_id": "op1",
         "media_type": "chat", "ack": 3},
    ]
    score = score_agilidad(msgs)
    consejo = consejo_de("agilidad", score.rating_label)
    if consejo is not None:
        assert score.recomendacion_codigos == [consejo.codigo]
    else:
        assert score.recomendacion_codigos == []


# --- EL CONTRATO GENERICO: una sola fuente de verdad, rubrica por rubrica ---------------
# Va parametrizado sobre el catalogo, no sobre una lista fija: cada rubrica que se migre
# queda cubierta al agregarla, sin tocar este test. Es lo que impide que una rubrica se
# guarde su copia del texto y el catalogo empiece a mentir.
def test_ninguna_rubrica_migrada_conserva_su_propio_COACHING():
    """Si la rubrica ya esta en el catalogo, su modulo NO puede tener `_COACHING*` propio."""
    import importlib

    migradas = {c.rubrica for c in CONSEJOS}
    for rubrica in sorted(migradas):
        mod = importlib.import_module(f"src.{rubrica}")
        sobrantes = [a for a in dir(mod) if a.startswith("_COACHING")]
        assert not sobrantes, (
            f"src/{rubrica}.py sigue teniendo {sobrantes}: son dos fuentes de verdad y el "
            f"catalogo miente en cuanto una cambie")


def test_cada_situacion_del_catalogo_es_unica_por_rubrica_y_segmento():
    """El SEGMENTO entra en la clave desde el 2026-08-21: el mismo (rubrica, situacion) tiene
    una variante por segmento a proposito -- `info/4` le pide al jugador la pregunta de
    cierre y al agente /FIN mas los 5 minutos, porque el manual los trata distinto. Lo que
    sigue sin poder repetirse es la tripleta completa."""
    vistos = set()
    for c in CONSEJOS:
        clave = (c.rubrica, c.situacion, c.segmento)
        assert clave not in vistos, f"{clave} duplicada: consejo_de() devolveria uno al azar"
        vistos.add(clave)


# --- EL MISMO MOTIVO, OTRO SEGMENTO ------------------------------------------------------
# `info` empezo a cubrir las consultas del AGENTE (comision, diseño, interesado en ser
# agente, Back Office...) por decision del negocio del 2026-08-21. Pero sus textos estaban
# escritos para el jugador: "quien pregunta todavia esta decidiendo si se queda" (C06),
# "quien consulta esta comparando" (C07). Un agente que pregunta por su comision es un socio
# con contrato, no un prospecto -- emitirle eso es coaching falso.
# Y NO ES SOLO REDACCION: el manual le da al agente una regla de cierre DISTINTA. "Debido a
# que muchos no responden despues de recibir la informacion, el operador PUEDE cerrar el chat
# cuando el caso haya sido resuelto", con /Fin y 5 minutos. Al agente no se lo presiona con
# la pregunta de cierre como al jugador.
def test_info_tiene_variante_de_agente_para_las_cuatro_situaciones():
    for sit in ("1", "2", "3", "4"):
        c = consejo_de("info", sit, segmento="agente")
        assert c is not None, f"info/{sit} sin variante de agente"
        assert c.segmento == "agente"


def test_el_default_sigue_siendo_el_del_jugador():
    """Los 6 modulos que ya llaman `consejo_de(rubrica, situacion)` no se tocan."""
    c = consejo_de("info", "3")
    assert c is not None
    assert c.segmento == "jugador"


def test_la_variante_de_agente_no_habla_de_prospectos():
    """El framing de adquisicion no aplica a un socio con contrato."""
    prohibido = ("decidiendo si se queda", "esta comparando", "si se queda")
    for sit in ("1", "2", "3", "4"):
        t = consejo_de("info", sit, segmento="agente").texto.lower()
        for p in prohibido:
            assert p not in t, f"info/{sit} de agente sigue hablandole a un prospecto: {t}"


def test_el_cierre_del_agente_no_le_exige_la_pregunta():
    """El manual permite cerrar al agente sin esperar respuesta. Pedirle "¿te falta algo
    más?" seria exigirle algo que el propio manual releva."""
    t = consejo_de("info", "4", segmento="agente").texto.lower()
    assert "algo más" not in t
    assert "/fin" in t or "5 minutos" in t


def test_cada_variante_de_agente_apunta_a_una_practica_del_manual():
    for sit in ("1", "2", "3", "4"):
        c = consejo_de("info", sit, segmento="agente")
        assert c.practica in CODIGOS_PRACTICA


def test_la_clave_es_rubrica_situacion_y_segmento():
    """Sin el segmento en la clave, la variante de agente pisaria la del jugador."""
    jug = consejo_de("info", "3", segmento="jugador")
    age = consejo_de("info", "3", segmento="agente")
    assert jug.codigo != age.codigo
    assert jug.texto != age.texto
