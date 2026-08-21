"""`no_agent_reply` deja de ser un SKIP y pasa a llevar nota. Decision del negocio (2026-08-21).

LO QUE EL SKIP ESTABA ESCONDIENDO. Son **1.167 sesiones** donde el cliente escribio y NADIE
del negocio contesto. Hoy se saltean, asi que la peor falla que este sistema puede medir es
justamente la unica que no aparece en ningun cuadro.

Y NO ES NEGLIGENCIA PASIVA. Medido sobre 300 de esas sesiones:
    100,0%  tienen notas del CRM
     99,7%  el nombre del operador sale de una nota "<Nombre> *resuelto* la conversacion"
      0,0%  tienen UN SOLO mensaje del negocio
     75,0%  el cliente se quedo con la ultima palabra
O sea: **un operador marco la conversacion como resuelta sin escribirle nunca al cliente.**
Es una accion deliberada y atribuible, no un descuido. Ejemplos reales de la nota:
    "Mel *resuelto* la conversación"
    "*Asignado automáticamente* a Maria Jose" + "Maria Jose *resuelto* la conversación"

EL MANUAL LO TIPIFICA TRES VECES:
  E06  "Cerrar chats sin seguimiento adecuado o sin despedida. Cada conversacion debe
        cerrarse con un mensaje claro, cordial y profesional."
  B10  el minuto de primera respuesta, que el manual fija dos veces.
       "Es politica obligatoria del departamento que el ultimo mensaje siempre sea enviado
        por el operador."

POR QUE 1 ESTRELLA Y NO UN SKIP CON ETIQUETA. Un skip dice "no habia nada que evaluar", y
aca habia todo: un cliente esperando. La atribucion es solida -- la nota del CRM acierta el
99% contra la verdad conocida (ver las seis puertas de src/operators.py), y ademas es la
persona que EJECUTO el cierre, no la que "tenia" la conversacion.

LA CAUSA NO SE PIERDE. El CHECK de conversation_scores exige `skip_reason IS NULL` en las
filas evaluadas, asi que la etiqueta desaparece de ese filtro del tablero -- y esta bien,
porque dejan de ser "sin evaluar". Para que un supervisor las siga aislando, la razon viaja
en `dimensions.sin_respuesta_del_negocio`.
"""
from datetime import datetime, timedelta, timezone

from src.sin_respuesta import hubo_respuesta_del_negocio, score_sin_respuesta

BASE = datetime(2026, 3, 10, 16, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="hola, no me entra la recarga", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media, "sent_from": None,
            "user_id": None, "ack": 3}


def _op(minutos, body="ya te ayudo"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "WEB",
            "user_id": "op1", "ack": 3}


def _nota(minutos, body="Mel *resuelto* la conversación"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": body, "media_type": None, "sent_from": None,
            "user_id": None, "ack": None}


# --- el predicado -----------------------------------------------------------------------
def test_sin_un_solo_mensaje_del_negocio_no_hubo_respuesta():
    assert hubo_respuesta_del_negocio([_cli(0), _cli(5, "hola?"), _nota(9)]) is False


def test_la_nota_del_crm_NO_cuenta_como_respuesta():
    """LA LECCION QUE YA COSTO CARO (ver `cliente_tuvo_la_ultima_palabra`): la nota es
    `from_me` pero NO es un mensaje al cliente. Contarla convertiria justo estas sesiones
    en "si respondio", que es el bug que el skip venia tapando."""
    assert hubo_respuesta_del_negocio([_cli(0), _nota(3)]) is False


def test_un_mensaje_del_operador_ya_es_respuesta():
    assert hubo_respuesta_del_negocio([_cli(0), _op(1)]) is True


def test_el_bot_tambien_cuenta_como_respuesta():
    """Criterio conservador: si el bot contesto, la sesion NO es "nadie respondio". Que el
    bot no sea merito es otro problema (ver `metrics.hay_persona_del_negocio`); aca lo que
    se mide es si el cliente quedo sin NINGUNA respuesta."""
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True, "is_note": False,
           "body": "menu: 1) recargas 2) retiros", "media_type": "chat",
           "sent_from": "CHATBOT", "user_id": None, "ack": 3}
    assert hubo_respuesta_del_negocio([_cli(0), bot]) is True


# --- la nota -----------------------------------------------------------------------------
def test_nadie_respondio_es_una_estrella():
    r = score_sin_respuesta([_cli(0), _cli(5, "hola?"), _nota(9)])
    assert r is not None
    assert r.stars == 1
    assert r.rating_label == "mala"


def test_el_rationale_dice_que_la_cerraron_sin_contestar():
    """Un supervisor tiene que poder leer la fila y entender el hecho, sin abrir el chat."""
    r = score_sin_respuesta([_cli(0), _cli(5, "hola?"), _nota(9)])
    bajo = r.rating_rationale.lower()
    assert "respond" in bajo or "contest" in bajo
    assert "cerr" in bajo, r.rating_rationale


def test_la_causa_viaja_en_dimensions():
    """El CHECK de la tabla borra `skip_reason` en las filas evaluadas, asi que la razon
    tiene que quedar en otro lado para que el tablero las siga aislando."""
    r = score_sin_respuesta([_cli(0), _nota(3)])
    assert r.dimensions.get("sin_respuesta_del_negocio") is True


def test_cede_el_turno_si_alguien_respondio():
    """La rubrica no se mete donde no corresponde: devuelve None igual que las otras."""
    assert score_sin_respuesta([_cli(0), _op(1)]) is None


def test_sin_mensajes_no_rompe():
    assert score_sin_respuesta([]) is None


def test_no_inventa_coaching_generico():
    """El consejo tiene que nombrar el hecho, no dar una frase de relleno."""
    r = score_sin_respuesta([_cli(0), _nota(3)])
    assert r.recomendacion
    assert "respond" in r.recomendacion.lower() or "contest" in r.recomendacion.lower()


def test_no_declara_un_motivo_que_no_puede_saber():
    """Nadie contesto: no hay conversacion de la que inferir un motivo, y ponerle uno seria
    inventarlo. La falla es ANTERIOR a cualquier motivo."""
    r = score_sin_respuesta([_cli(0), _nota(3)])
    assert r.motivo is None
