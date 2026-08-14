"""El rationale no puede desmentir a una señal DURA.

MEDIDO el 2026-08-14 sobre el rescore v15 (15.562 filas evaluadas): de las 2.311 filas de
`registro` que caen al camino LLM, **283 traen un rationale que afirma que no se pidieron
los datos**. Corriendo `fue_al_punto` -la señal determinista- sobre los mensajes reales:

  - **149 (52,7%) lo afirman EN FALSO**: el operador SI pidio los datos. Reparto por
    etiqueta: 134 'buena', 10 'aceptable', 5 'deficiente'.
  - 134 lo afirman con razon.

El operador lee una acusacion falsa pegada a una nota que dice que hizo bien el trabajo.
La ESTRELLA esta bien -la protege el piso determinista-; lo que miente es el texto.

POR QUE NO SE FILTRA EL RATIONALE CON `_CONTRADICE_RE`, que ya existe. Ese patron nacio
para la nota de evidencia POR DIMENSION que alimenta `aciertos[]`, donde un "pero" invalida
el acierto. El `rating_rationale` es la justificacion GENERAL: ahi un "pero" es prosa
normal. Medido el mismo dia: `_CONTRADICE_RE` matchea el **78,1%** de los rationales
'buena' (1.380 de 1.766) y el **92,3%** de los 'deficiente' (253 de 274). Aplicarlo en
bloque borraria el texto de casi todo el padron, incluidas las 134 afirmaciones ciertas.

Por eso el guard es QUIRURGICO: un reclamo puntual, desmentido por una señal dura puntual.
No se borra el texto del modelo -se conserva entero- y se le anexa la correccion, para que
quien lo lee vea las dos cosas.
"""
from src.registro import rationale_desmiente_el_pedido
from src.scorer import score_by_motivo

# El operador PIDE LOS DATOS: dispara `fue_al_punto` por el grupo del formulario.
PIDIO_LOS_DATOS = [
    {"from_me": False, "is_note": False, "body": "hola quiero registrarme"},
    {"from_me": True, "is_note": False,
     "body": "Ayudame con los datos para tu registro: Nombre de usuario, "
             "Correo electronico y numero de celular"},
]
# El operador solo recita la plantilla de venta: `fue_al_punto` da False.
SOLO_PLANTILLA = [
    {"from_me": False, "is_note": False, "body": "hola quiero registrarme"},
    {"from_me": True, "is_note": False,
     "body": "Tenemos un bono por tu primera recarga, aprovecha la promo"},
]

RECLAMO = ("El operador atendio el motivo de registro, pero no se pidieron los datos "
           "necesarios para crear la cuenta")
MARCA_CORRECCION = "[ajuste determinista de hechos: el operador SI pidio los datos del alta]"


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over):
    resp = {
        "motivo": "registro",
        "dimensions": {"resolucion": "guio el alta", "iniciativa": "no ofrecio nada extra",
                       "cortesia": "cordial", "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "rating_rationale": RECLAMO,
        "recomendacion": "pedile los datos",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# --- el predicado, aislado -------------------------------------------------------

def test_el_reclamo_es_falso_cuando_el_operador_pidio_los_datos():
    assert rationale_desmiente_el_pedido(RECLAMO, PIDIO_LOS_DATOS) is True


def test_el_reclamo_es_cierto_cuando_solo_hubo_plantilla_de_venta():
    # Las 134 filas que lo afirman CON RAZON: no se toca su texto.
    assert rationale_desmiente_el_pedido(RECLAMO, SOLO_PLANTILLA) is False


def test_un_rationale_que_no_reclama_nada_no_se_anota():
    assert rationale_desmiente_el_pedido(
        "El operador guio el alta y encamino el primer deposito", PIDIO_LOS_DATOS) is False


def test_un_rationale_vacio_no_rompe():
    assert rationale_desmiente_el_pedido("", PIDIO_LOS_DATOS) is False
    assert rationale_desmiente_el_pedido(None, PIDIO_LOS_DATOS) is False


# --- integrado en el scorer ------------------------------------------------------

def test_el_scorer_anota_la_correccion_sin_borrar_el_texto_del_modelo():
    r = score_by_motivo(target_messages=PIDIO_LOS_DATOS, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.llm_model == "qwen3.5:4b", "debe caer al camino LLM, no al determinista"
    # El texto del modelo se CONSERVA entero.
    assert RECLAMO in r.rating_rationale
    # Y queda anotado que ese reclamo es falso.
    assert MARCA_CORRECCION in r.rating_rationale


def test_el_scorer_no_anota_nada_cuando_el_reclamo_es_cierto():
    # Las 134 filas donde el modelo tiene razon: su texto queda intacto.
    r = score_by_motivo(target_messages=SOLO_PLANTILLA, thread_context="",
                        llm=FakeLLM(_resp()))
    assert MARCA_CORRECCION not in r.rating_rationale


def test_la_correccion_no_mueve_la_estrella():
    con_reclamo = score_by_motivo(target_messages=PIDIO_LOS_DATOS, thread_context="",
                                  llm=FakeLLM(_resp()))
    sin_reclamo = score_by_motivo(
        target_messages=PIDIO_LOS_DATOS, thread_context="",
        llm=FakeLLM(_resp(rating_rationale="El operador guio el alta paso a paso")))
    assert con_reclamo.stars == sin_reclamo.stars
    assert con_reclamo.rating_label == sin_reclamo.rating_label
