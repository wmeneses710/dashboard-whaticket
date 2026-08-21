"""El último mensaje tiene que ser del operador, y el quinto lucero no se regala.

Manual de ATC, TEXTUAL en el cap. 04 y otra vez en el cap. 06:

    "Es política obligatoria del departamento que el último mensaje de la conversación
     sea enviado por el operador."

Y por si quedaba duda sobre el caso chico:

    "Si, después de haber resuelto la solicitud y enviado la despedida, el cliente responde
     con un 'gracias', emoji, sticker u otro mensaje breve, el operador deberá responder
     para mantener el estándar de cierre."

QUE HACE ESTA SEÑAL Y QUE NO. NO baja notas: BLOQUEA LA QUINTA ESTRELLA. El texto del 5 en
las cuatro rubricas que lo dan por el cierre dice, literal, "antes de cerrar se aseguró de
que no le faltara nada" -- y eso no se puede afirmar de una sesion donde el cliente escribio
DESPUES del operador y nadie le contesto. La fila se desmentiria sola, que es la familia de
bug que este repo ya pago cara dos veces.

EL GATE DEL CIERRE ES LO QUE LA VUELVE JUSTA. Solo cuenta si el cliente quedo sin respuesta
con el ticket TODAVIA ABIERTO. MEDIDO el 2026-08-19: de las 659 sesiones de 5 estrellas que
terminan con el cliente, **548 (83%) escribieron DESPUES de `resolved_at`** -- o sea que el
operador ya habia mandado /FIN, esperado sus 5 minutos y cerrado, que es exactamente el
procedimiento del manual. Castigar eso seria castigar al que cumplio. Quedan 111.

LA POBLACION, para que se lea la proporcion: solo el 4,1% de las 65.588 sesiones evaluadas
termina con el cliente. El cumplimiento de esta politica ya es alto; lo que se corrige es el
puñado que ademas se llevaba la nota maxima. Y lo que manda el cliente es casi siempre una
cortesia -- "gracias" 620, "muchas gracias" 202, "ok" 157, stickers 72, "listo" 67 --, que es
justo el caso que el manual obliga a contestar.

AGILIDAD QUEDA AFUERA, a proposito. La politica del manual tambien cubre a los agentes, pero
esa rubrica mide UNA sola cosa por diseño -- cuanto tardo el operador-- y meterle un eje de
cortesia la convierte en otra cosa. Son ~1.263 sesiones de agente (5%) y entrarlas es una
decision del negocio, no un arreglo. Ver el docstring de src/agilidad.py.
"""
from datetime import datetime, timedelta, timezone

from src.signals import cliente_tuvo_la_ultima_palabra

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="gracias", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="Listo, ya está", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


def _nota(minutos, body="Ana *resolvió* la conversación"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": body}


CIERRE = BASE + timedelta(minutes=20)


# --- el caso ----------------------------------------------------------------------

def test_el_cliente_escribe_ultimo_con_el_ticket_abierto():
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "gracias")]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is True


def test_el_operador_contesta_la_cortesia_y_queda_bien():
    """Lo que el manual pide: un mensaje corto, un emoji o un sticker alcanzan."""
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "gracias"), _op(3, "Con gusto 😊")]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is False


def test_un_sticker_del_cliente_tambien_cuenta():
    """El manual nombra el sticker explicitamente. Ojo: `is_real_media` ya NO trata al
    sticker como adjunto (v17), pero acá lo que importa es QUIEN hablo último."""
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "", media="sticker")]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is True


# --- el gate del cierre, que es lo que la vuelve justa ------------------------------

def test_si_el_cliente_escribio_DESPUES_del_cierre_no_cuenta():
    """El operador mandó /FIN, esperó sus 5 minutos y cerró: hizo el procedimiento. En la
    data son 548 de las 659 sesiones de 5 estrellas que terminan con el cliente (83%)."""
    msgs = [_cli(0, "hola"), _op(1), _cli(25, "gracias")]   # el cierre fue en el minuto 20
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is False


def test_justo_en_el_cierre_no_cuenta():
    """Borde inclusivo hacia el operador: un mensaje simultáneo al cierre no prueba que lo
    haya ignorado."""
    msgs = [_cli(0, "hola"), _op(1), _cli(20, "gracias")]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is False


def test_sin_cierre_conocido_se_evalua_igual():
    """`cierre_at` puede faltar. Ahí no hay con qué exculpar, pero tampoco se inventa: se
    mira lo único que se sabe, que el cliente hablo último."""
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "gracias")]
    assert cliente_tuvo_la_ultima_palabra(msgs, None) is True


# --- los guards -------------------------------------------------------------------

def test_una_nota_interna_posterior_no_salva_al_operador():
    """La nota de cierre del CRM no es un mensaje al cliente: el cliente sigue sin respuesta."""
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "gracias"), _nota(3)]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is True


def test_una_sesion_que_termina_con_el_operador_esta_bien():
    msgs = [_cli(0, "hola"), _op(1), _cli(2, "y cuánto tarda"), _op(3, "Cinco minutos")]
    assert cliente_tuvo_la_ultima_palabra(msgs, CIERRE) is False


def test_sin_mensajes_no_hay_nada_que_afirmar():
    assert cliente_tuvo_la_ultima_palabra([], CIERRE) is False
    assert cliente_tuvo_la_ultima_palabra([_nota(1)], CIERRE) is False


# --- el efecto en las rubricas: bloquea el 5, no baja la nota -----------------------

def test_deposito_no_llega_a_5_si_dejo_al_cliente_hablando():
    from src.deposito import calificar_deposito
    base = [_cli(0, "hice mi recarga"), _cli(0, "", media="image"),
            _op(1, "Estamos verificando tu comprobante."),
            _op(2, "Gracias por tu recarga. Tu saldo ya esta disponible."),
            _op(3, "¿Hay algo mas en lo que te pueda ayudar?")]
    limpia = calificar_deposito(base + [_cli(9, "no, gracias"), _op(10, "Con gusto 😊")], CIERRE)
    colgada = calificar_deposito(base + [_cli(9, "no, gracias")], CIERRE)
    assert limpia.stars == 5, "cerró bien: el 5 se mantiene"
    assert colgada.stars == 4, "dejó al cliente hablando: no hay 5"
    assert "última palabra" in colgada.rationale or "sin respuesta" in colgada.rationale


def test_el_5_bloqueado_NO_baja_de_4():
    """Es un TECHO, no un castigo: el trabajo se hizo y la nota lo refleja."""
    from src.deposito import calificar_deposito
    msgs = [_cli(0, "hice mi recarga"), _cli(0, "", media="image"),
            _op(1, "Estamos verificando tu comprobante."),
            _op(2, "Gracias por tu recarga. Tu saldo ya esta disponible."),
            _op(3, "¿Hay algo mas en lo que te pueda ayudar?"), _cli(9, "listo gracias")]
    assert calificar_deposito(msgs, CIERRE).stars == 4
