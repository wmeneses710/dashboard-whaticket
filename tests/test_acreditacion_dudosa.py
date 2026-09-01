"""CAPA 2: el modelo decide si el operador confirmo la acreditacion cuando el patron no la vio.

POR QUE HACE FALTA. `deposito.py` afirma "nunca le confirmo al cliente que la plata habia
entrado" apoyandose en `signals.operator_acreditacion`, que es un patron. Y UN PATRON PUEDE
PROBAR PRESENCIA, NUNCA AUSENCIA: que no matcheo y que no existe no son la misma frase. Los
cinco huecos de vocabulario que se taparon entre el 2026-08-11 y el 2026-09-01 fueron los
cinco del mismo lado -- ninguno afirmo de mas, todos afirmaron de menos.

MEDIDO CONTRA gemma4:12b el 2026-09-01, 304 interacciones reales de `deposito` en dos grupos:

  GRUPO NEG (151) -- las que hoy salen en 2 estrellas por "nunca le confirmo":
      tienen confirmacion REAL .................... 102 de 151 (67,5%)
      el modelo contradice al patron en la
      direccion peligrosa (patron si / modelo no) .   0 de 151   <- el error CARO
      el modelo encuentra lo que el patron
      todavia pierde ..............................  20, con cita verificable 20/20

  GRUPO POS (153) -- control, las que el patron YA ve:
      el modelo las NEGARIA ....................... 39 de 153 (25,5%)
      de esas, son jerga del CRM ("ing") .......... 38 de 39

O sea: el patron sabe el VOCABULARIO DEL NEGOCIO ("ing" no es español, es taquigrafia de
este CRM) y el modelo sabe el IDIOMA (agarra "Se ecuentra lista su saldo", con la falta de
ortografia adentro). Por eso se pregunta SOLO cuando el patron dice que no: los "ing" nunca
llegan al modelo y no se pierde la jerga. Un "todo al modelo" destruiria 1 de cada 4.

COSTO: 7,6 inferencias por dia sobre ~350 que ya corren (+2,2%).

LA CITA ES EL CINTURON. Se le exige al modelo la frase EXACTA que prueba la confirmacion y
se verifica que exista en el texto del OPERADOR. En los 304 casos acerto 102 de 102, pero
la nota de una persona no se apoya en una racha: sin cita verificable, no hay confirmacion.
"""
import pytest

from src.acreditacion_dudosa import confirmo_segun_el_modelo, necesita_revisar


def _op(texto, media="chat"):
    return {"from_me": True, "is_note": False, "body": texto, "media_type": media,
            "created_at": None}


def _cli(texto, media="chat"):
    return {"from_me": False, "is_note": False, "body": texto, "media_type": media,
            "created_at": None}


class _LLM:
    """LLM de mentira. `respuesta` puede ser un dict o una excepcion a levantar."""

    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.llamadas = []

    def chat_json(self, system, user, schema=None):
        self.llamadas.append((system, user, schema))
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


# --- el gate: a quien SE LE PREGUNTA ------------------------------------------------------

def test_no_se_pregunta_si_el_operador_no_escribio_NADA():
    """Sin texto del operador no hay nada que leer: la ausencia ahi SI es verificable."""
    assert necesita_revisar([_cli("hola"), _cli("mi comprobante")]) is False


def test_no_se_pregunta_si_el_operador_solo_mando_adjuntos():
    assert necesita_revisar([_cli("hola"), _op("", media="image")]) is False


def test_no_se_pregunta_si_el_patron_YA_vio_la_confirmacion():
    """EL GUARD QUE PROTEGE LA JERGA. 38 de los 39 desacuerdos del control son 'ing':
    si se le preguntara igual, el modelo destruiria 1 de cada 4 confirmaciones buenas."""
    assert necesita_revisar([_cli("comprobante"), _op("ing")]) is False
    assert necesita_revisar([_cli("comprobante"), _op("Listo mi amigo")]) is False


def test_se_pregunta_cuando_el_operador_hablo_y_el_patron_no_vio_nada():
    assert necesita_revisar([_cli("comprobante"), _op("Estamos realizando su recarga")]) is True


# --- la decision ---------------------------------------------------------------------------

def test_sin_llm_no_se_decide_nada():
    assert confirmo_segun_el_modelo([_op("¡Todo listo!")], None) is None


def test_una_inferencia_que_FALLA_no_cambia_la_nota():
    """None y False no son lo mismo: None es un fallo y deja la nota como esta hoy."""
    for fallo in (RuntimeError("timeout"), ValueError("json roto")):
        assert confirmo_segun_el_modelo([_op("¡Todo listo!")], _LLM(fallo)) is None


def test_confirma_cuando_el_modelo_lo_dice_Y_la_cita_existe():
    llm = _LLM({"confirmo": True, "frase": "¡Todo listo!"})
    assert confirmo_segun_el_modelo([_op("Buenas. ¡Todo listo! 🎉")], llm) is True


def test_la_cita_INVENTADA_no_confirma_nada():
    """El cinturon. Una tilde falsa le afirma al negocio algo que nunca paso, y eso es
    peor que una nota baja: ver el caso de `_PROMESA_1A_RE` en signals.py."""
    llm = _LLM({"confirmo": True, "frase": "su recarga ya fue acreditada"})
    assert confirmo_segun_el_modelo([_op("Estamos realizando su recarga")], llm) is None


def test_la_cita_del_CLIENTE_no_sirve_de_prueba():
    """Lo que se juzga es lo que dijo el OPERADOR. Que el cliente diga 'ya me llego'
    no es una confirmacion del operador."""
    llm = _LLM({"confirmo": True, "frase": "ya me llego"})
    assert confirmo_segun_el_modelo(
        [_cli("ya me llego"), _op("Estamos realizando su recarga")], llm) is None


def test_el_modelo_puede_decir_que_NO():
    llm = _LLM({"confirmo": False, "frase": ""})
    assert confirmo_segun_el_modelo([_op("Estamos realizando su recarga")], llm) is False


def test_una_respuesta_fuera_de_forma_es_un_fallo_y_no_una_confirmacion():
    for basura in ({}, {"confirmo": "quiza"}, {"frase": "listo"}, None, []):
        assert confirmo_segun_el_modelo([_op("¡Todo listo!")], _LLM(basura)) is not True


@pytest.mark.parametrize("frase,texto", [
    # Los 5 giros REALES que el patron pierde y el modelo agarro, con su cita verificada.
    ("recargado en tu cuenta amigo", "ya te cargo amigo recargado en tu cuenta amigo"),
    ("Se ecuentra lista su saldo mi estimada", "Buenos dias. Se ecuentra lista su saldo mi estimada"),
    ("¡Todo listo! 🎉", "Estamos realizando su recarga. ¡Todo listo! 🎉"),
    ("hecho amigo", "te paso cuenta pichincha? hecho amigo"),
    ("ya tienes el saldo amigo", "dame un ratito amigo ya tienes el saldo amigo"),
])
def test_los_giros_reales_que_el_patron_pierde(frase, texto):
    assert confirmo_segun_el_modelo([_op(texto)], _LLM({"confirmo": True, "frase": frase})) is True
