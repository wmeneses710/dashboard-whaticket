"""Un mensaje del negocio SIN una persona detras no es atencion de un operador.

`_is_bot` mira SOLO `sent_from='CHATBOT'`, asi que otras dos poblaciones sin persona
contaban como trabajo humano -- en la nota Y en el chat del modal, donde salian rotuladas
"OPERADOR".

MEDIDO el 2026-08-14 sobre la copia, mensajes del negocio (`from_me`, sin notas):

    sent_from      mensajes     con user_id    cuerpos distintos
    WEB           1.138.463     1.138.313          353.567
    NULL            236.259       230.773            1.932
    CHATBOT           5.967             0               28
    api               1.167             0              271

`CHATBOT` no tiene NI UN mensaje con `user_id` y repite 28 textos, que son menus numerados
("¡Hola! Para brindarte una mejor atención, selecciona una opción: *1*..."). `api` es
marketing masivo ("*¡Aficionados al fútbol, la emoción está por comenzar en Sorti!*"). Las
plantillas REALES del operador viven en `WEB`, con user_id y 353.567 cuerpos distintos: son
poblaciones que no se tocan.

IMPACTO A NIVEL SESION: de 16.896 evaluadas, **4** tienen como unico "operador" mensajes sin
persona detras, y **1** de esas llego a 4 o 5 estrellas. Chico, pero una nota alta sobre una
sesion donde nadie trabajo es el dato mas corrosivo que puede tener el tablero.

LO QUE **NO** SE HACE: saltear esas sesiones. `1bd61c16` es una persona que escribio 13
veces y solo le contesto un bot -- ese 1 estrella es un problema REAL y saltearlo lo
esconderia. El guard mata el merito que nadie se gano, no el reclamo legitimo.
"""
from src.metrics import hay_persona_del_negocio, sin_persona_detras


def _msg(**kw):
    base = {"from_me": True, "is_note": False, "body": "hola", "sent_from": "WEB",
            "user_id": "op1", "media_type": "chat"}
    base.update(kw)
    return base


# --- quien NO tiene una persona detras -------------------------------------------

def test_el_chatbot_no_tiene_persona():
    assert sin_persona_detras(_msg(sent_from="CHATBOT", user_id=None)) is True


def test_el_marketing_por_api_tampoco():
    assert sin_persona_detras(_msg(sent_from="api", user_id=None)) is True


def test_un_mensaje_SIN_REMITENTE_no_se_asume_maquina():
    """El discriminador es el REMITENTE, no la falta de `user_id`.

    Este repo ya midio que **230.773 mensajes de operadores reales vienen sin `sent_from`**,
    y las seis puertas de atribucion existen porque hay que rescatar por firma o por nota a
    los que no traen `user_id`: de 882 sesiones sin user_id ni firma, la nota rescata 860.
    Un campo vacio no es evidencia de una maquina. La primera version de este guard lo
    asumia y se llevaba puestos los fixtures de medio repo -- y, peor, sesiones reales.
    """
    assert sin_persona_detras(_msg(sent_from=None, user_id=None)) is False


# --- quien SI ---------------------------------------------------------------------

def test_una_plantilla_del_operador_SI_tiene_persona():
    # Es el caso que hay que no romper: las plantillas reales viven en WEB con user_id.
    plantilla = _msg(sent_from="WEB", user_id="op1",
                     body="✨ _Gracias por comunicarte con nosotros 🙌")
    assert sin_persona_detras(plantilla) is False


def test_un_mensaje_sin_remitente_PERO_con_user_id_si_tiene_persona():
    assert sin_persona_detras(_msg(sent_from=None, user_id="op1")) is False


def test_el_mensaje_del_CLIENTE_no_entra_en_la_pregunta():
    assert sin_persona_detras(_msg(from_me=False, user_id=None, sent_from=None)) is False


def test_una_NOTA_tampoco():
    assert sin_persona_detras(_msg(is_note=True, user_id=None, sent_from=None)) is False


# --- a nivel sesion ---------------------------------------------------------------

def test_una_sesion_atendida_por_una_persona():
    msgs = [_msg(from_me=False, user_id=None), _msg()]
    assert hay_persona_del_negocio(msgs) is True


def test_una_sesion_donde_solo_contesto_el_bot():
    """El caso `c1034a14`: menu de chatbot puro, y salio con 5 estrellas por
    "el operador confirmó la operación con una respuesta implícita"."""
    msgs = [
        _msg(from_me=False, user_id=None, sent_from=None, body="/start"),
        _msg(sent_from="CHATBOT", user_id=None,
             body="Panita como te ayudo hoy 😎\n1. Recargar\n2. Quiero un usuario"),
        _msg(from_me=False, user_id=None, sent_from=None, body="1"),
        _msg(sent_from="CHATBOT", user_id=None, body="Panita como te ayudo hoy 😎"),
    ]
    assert hay_persona_del_negocio(msgs) is False


def test_una_sesion_donde_solo_salio_marketing():
    msgs = [
        _msg(from_me=False, user_id=None, sent_from="WEB", body="hola"),
        _msg(sent_from="api", user_id=None, body="*¡Aficionados al fútbol!* ⚽️"),
    ]
    assert hay_persona_del_negocio(msgs) is False


def test_una_sesion_sin_ningun_mensaje_del_negocio():
    # No hay persona, y esta bien: lo resuelve `decide_eligibility` con no_agent_reply.
    assert hay_persona_del_negocio([_msg(from_me=False, user_id=None)]) is False


# --- cableado: la nota no premia lo que nadie hizo --------------------------------

class _FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over):
    resp = {
        "motivo": "info",
        "dimensions": {"resolucion": "confirmo la operacion", "iniciativa": "atendio rapido",
                       "cortesia": "calido", "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": True,
        "cortesia_destacada": True,
        "hubo_maltrato_grave": False,
        "cliente_reinsistio": False,
        "rating_rationale": "El operador atendió el motivo al confirmar la operación",
        "recomendacion": "",
        "atencion": "empujo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


SOLO_BOT = [
    _msg(from_me=False, user_id=None, sent_from=None, body="/start"),
    _msg(sent_from="CHATBOT", user_id=None, body="Panita como te ayudo hoy 😎\n1. Recargar"),
    _msg(from_me=False, user_id=None, sent_from=None, body="1"),
]
CON_PERSONA = [
    _msg(from_me=False, user_id=None, sent_from=None, body="una consulta sobre la app"),
    _msg(sent_from="WEB", user_id="op1", body="claro, te cuento como funciona"),
]


def test_sin_persona_la_nota_no_puede_ser_un_merito():
    from src.scorer import score_by_motivo

    r = score_by_motivo(target_messages=SOLO_BOT, thread_context="", llm=_FakeLLM(_resp()))
    assert r.rating_label not in ("excelente", "buena"), \
        f"label={r.rating_label} stars={r.stars} — 5 estrellas a nadie"
    assert r.stars <= 3


def test_con_persona_la_nota_no_se_toca():
    from src.scorer import score_by_motivo

    r = score_by_motivo(target_messages=CON_PERSONA, thread_context="", llm=_FakeLLM(_resp()))
    assert r.rating_label == "excelente" and r.stars == 5


def test_el_texto_dice_que_no_hubo_nadie():
    from src.scorer import score_by_motivo

    r = score_by_motivo(target_messages=SOLO_BOT, thread_context="", llm=_FakeLLM(_resp()))
    assert "no hubo ningún operador detrás" in r.rating_rationale, r.rating_rationale
