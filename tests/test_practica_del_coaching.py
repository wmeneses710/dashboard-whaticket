"""El coaching declara A QUE BUENA PRACTICA del manual apunta. Las dos mitades, igual.

EL PROBLEMA. La recomendacion del LLM son **12.163 filas con 10.325 textos distintos (84,9%
unicos)**: incontable. Un supervisor no puede decir "esta semana el coaching apunto 40 veces a
lo mismo". Es identicamente el problema que tenian los `errores` antes de v21 (7.019 errores en
3.680 textos).

POR QUE NO SE REEMPLAZA LA PROSA POR EL CODIGO, que era la idea original y esta MAL. Los B son
practicas genericas:

    B02  "Responder de forma clara, directa y ordenada."

Eso dice QUE, no COMO. Y este repo tiene un invariante que exige lo contrario
(`test_el_coaching_dice_COMO_no_solo_QUE_paso`): un consejo que no dice como hacerlo no sirve
al operador. Cambiar la prosa por "B02" haria la fila mas contable y el coaching MENOS util.
Asi que van las dos cosas: **el codigo hace la fila sumable, la prosa la hace accionable.** Es
lo mismo que v21 hizo con los errores -- el codigo E0x MAS la frase del manual.

LA CUENTA QUEDA UNIFORME ENTRE LOS DOS CAMINOS, que es lo que la vuelve util. El 85% del
coaching es determinista y su catalogo ya ata cada consejo a un B (`Consejo.practica`); el 15%
que escribe el LLM ahora declara el suyo. Sin eso, sumar por practica solo cubriria una parte y
el numero mentiria por omision.

AL MODELO SE LE DAN LOS CODIGOS CON LA FRASE, y es la leccion de v21 escrita en
`catalogo_atc.bloque_para_el_prompt`: "sin la frase, un codigo suelto es una etiqueta que cada
corrida interpreta distinto".
"""
from src.catalogo_atc import CODIGOS_PRACTICA, bloque_practicas_para_el_prompt
from src.catalogo_coaching import CONSEJOS


# --- el bloque del prompt ---------------------------------------------------------------
def test_el_bloque_lleva_los_doce_codigos_con_su_frase():
    bloque = bloque_practicas_para_el_prompt()
    for codigo in CODIGOS_PRACTICA:
        assert codigo in bloque, f"falta {codigo} en el bloque del prompt"
    # y la FRASE, no solo el numero: es lo que le fija el criterio al modelo
    assert "Responder de forma clara, directa y ordenada" in bloque
    assert "Cumplir con los tiempos de respuesta establecidos" in bloque


def test_el_prompt_le_pide_la_practica_al_modelo():
    from src.prompts import build_motivo_prompt

    system, _ = build_motivo_prompt(
        [{"from_me": False, "body": "hola", "is_note": False}], "")
    assert "recomendacion_practica" in system
    # el catalogo entero tiene que estar a la vista, si no elige a ciegas
    assert "B10" in system and "B02" in system


def test_el_schema_cierra_la_practica_al_catalogo():
    from src.prompts import build_motivo_schema

    props = build_motivo_schema()["properties"]
    enum = props["recomendacion_practica"]["enum"]
    for codigo in CODIGOS_PRACTICA:
        assert codigo in enum
    # el vacio tiene que ser valido: cinco estrellas no lleva consejo, y forzar un codigo
    # ahi inventaria una practica incumplida que no existe
    assert "" in enum


# --- las dos mitades declaran lo mismo --------------------------------------------------
def test_el_catalogo_determinista_ya_declara_su_practica():
    """La mitad determinista no cambia: ya lo tenia. Este test la ata al mismo vocabulario."""
    for c in CONSEJOS:
        assert c.practica in CODIGOS_PRACTICA


def test_la_rubrica_determinista_persiste_la_practica():
    """Sin esto, sumar por practica cubriria solo el camino del LLM -- el 15% -- y el numero
    mentiria por omision justo del lado que mas volumen tiene."""
    from datetime import datetime, timedelta, timezone

    from src.agilidad import score_agilidad
    from src.catalogo_coaching import consejo_de

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
    assert score.recomendacion_practica == (consejo.practica if consejo else "")


def test_sin_consejo_no_hay_practica():
    """Cinco estrellas no lleva consejo, asi que tampoco practica incumplida."""
    from src.scorer import ScoreResult

    r = ScoreResult(rubric="info", dimensions={}, rating_label="excelente",
                    rating_rationale="ok", stars=5, llm_model="x",
                    atencion=None, deposit_observed=None)
    assert r.recomendacion_practica == ""


# --- el camino del LLM ------------------------------------------------------------------
def test_el_scorer_captura_la_practica_que_eligio_el_modelo():
    from src.scorer import score_by_motivo

    class _LLM:
        model = "fake"

        def chat_json(self, system, user, schema=None):
            return {
                "motivo": "problema",
                "dimensions": {"resolucion": "atendio", "iniciativa": "-",
                               "cortesia": "cordial", "errores": []},
                "atendio_el_motivo": True, "hizo_accion_extra": False,
                "cortesia_destacada": False, "hubo_maltrato_grave": False,
                "claridad": "claro", "cliente_reinsistio": False,
                "rating_rationale": "resolvio el reclamo",
                "recomendacion": "Conviene confirmarle al cliente que el caso quedó cerrado.",
                "recomendacion_practica": "B12",
                "atencion": "empujo", "deposit_observed": False,
            }

    msgs = [{"created_at": None, "from_me": False, "is_note": False,
             "body": "me cobraron dos veces", "media_type": "chat"}]
    r = score_by_motivo(target_messages=msgs, thread_context="", llm=_LLM())
    assert r.recomendacion_practica == "B12"


def test_una_practica_invalida_del_modelo_se_descarta():
    """Un codigo inventado no puede viajar a la fila: el tablero lo sumaria como si
    existiera. Se degrada a vacio, igual que `atencion` fuera del enum."""
    from src.scorer import score_by_motivo

    class _LLM:
        model = "fake"

        def chat_json(self, system, user, schema=None):
            return {
                "motivo": "problema",
                "dimensions": {"resolucion": "atendio", "iniciativa": "-",
                               "cortesia": "cordial", "errores": []},
                "atendio_el_motivo": True, "hizo_accion_extra": False,
                "cortesia_destacada": False, "hubo_maltrato_grave": False,
                "claridad": "claro", "cliente_reinsistio": False,
                "rating_rationale": "resolvio el reclamo",
                "recomendacion": "algo",
                "recomendacion_practica": "B99",   # no existe
                "atencion": "empujo", "deposit_observed": False,
            }

    msgs = [{"created_at": None, "from_me": False, "is_note": False,
             "body": "me cobraron dos veces", "media_type": "chat"}]
    r = score_by_motivo(target_messages=msgs, thread_context="", llm=_LLM())
    assert r.recomendacion_practica == ""
