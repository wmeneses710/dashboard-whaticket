"""El segmento `agente` empieza a tener MOTIVO. Decision del negocio (2026-08-21).

DE DONDE SALE LA LISTA. El manual tiene una seccion literal, "Procesos que si gestionamos
para agentes": procesamiento de recargas, procesamiento de pagos, recepcion y tramitacion de
reclamos, solicitudes de diseño, solicitudes especiales de servicios activos, revision de
informacion o inconsistencias, y apoyo en solicitudes operativas autorizadas.

Contra nuestros ocho motivos, solo TRES mapean limpio -- `deposito` (recargas), `retiro`
(pagos) y `problema` (reclamos) -- y hoy no se aplica ninguno: **las 61.949 filas evaluadas
del segmento tienen `motivo = NULL`**, todas calificadas por `determinista/agilidad-v1`, que
mide unicamente el reloj.

LO DEMAS VA A `info`, POR DECISION DEL NEGOCIO: comision/meta/arrastre (1.383 sesiones,
2,0%), diseño (745), interesado en ser agente (437), datos o clave del agente en Back Office
(89), cierre o reingreso de agencia (21) e inconsistencias (8). El criterio es que todo eso
**es gente preguntando por algo**, y generalizar es preferible a inventar seis motivos con
poco volumen cada uno.

ESTE MODULO SOLO CLASIFICA. No cambia ninguna nota ni toca el ruteo del worker, y es a
proposito: la razon por la que existe `agilidad` es que correr el pase con LLM en el segmento
aplicaba la vara COMERCIAL del jugador (uplift, empujo/pasivo) y **topaba el 94% de las
sesiones de agente en 3 estrellas por diseño**. Re-rutear a ciegas repite ese error. Primero
se clasifica y se mide donde caerian las cosas; despues se decide el ruteo con el numero
adelante.

`problema` QUEDA AFUERA, y no por olvido: **no existe ninguna señal determinista de reclamo**
en todo el repo (cero funciones de reclamo o queja en src/signals.py). Es justamente el motivo
sin rubrica determinista, el unico que siempre cae al LLM. Meterlo aca exigiria una señal
nueva o el modelo, y las dos cosas son otro cambio.

OJO CON EL COACHING DE `info`: sus textos estan redactados para el jugador -- "quien pregunta
todavia esta decidiendo si se queda" (C06), "quien consulta esta comparando" (C07). Un agente
que pregunta por su comision no esta decidiendo si se queda ni comparando plataformas: es un
socio con contrato. Cuando el ruteo se active, `info` necesita variantes de agente en el
catalogo -- la clave del consejo es (rubrica, situacion) y da lugar para eso. Emitir el texto
de jugador a un agente seria coaching falso.
"""
from datetime import datetime, timedelta, timezone

from src.motivo_de_agente import motivo_de_agente

BASE = datetime(2026, 3, 10, 15, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media, "sent_from": None,
            "user_id": None, "ack": 3}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media, "sent_from": "WEB",
            "user_id": "op1", "ack": 3}


# --- los dos que se pueden PROBAR -------------------------------------------------------
def test_una_recarga_del_agente_es_deposito():
    """69,8% de las sesiones de agente tienen transaccion de deposito: es el grueso."""
    msgs = [_cli(0, "cargame 50 al usuario juan01"),
            _cli(1, "", media="image"),
            _op(2, "listo, ya quedo acreditado")]
    assert motivo_de_agente(msgs) == "deposito"


def test_un_pago_al_agente_es_retiro():
    """EL MANUAL LO LLAMA "procesamiento de pagos" Y EL AGENTE ESCRIBE "retiro".

    Medido sobre las sesiones del segmento: **8.862 usan retiro/retirar/sacar/cobrar** --
    lo que `retiro._MONTO_RE` ya detecta-- contra **986 que usan pago/pagar**, y de esas
    solo **64** traen un monto cerca. O sea el patron esta bien calibrado para la data y
    NO hay que ensancharlo por el vocabulario del manual: agregar "pago" suelto traeria
    el "el pago ya salio" del operador y el "pago movil" del medio de cobro.
    Las 64 quedan como hueco conocido y chico, no como bug.
    """
    msgs = [_cli(0, "necesito un retiro de 200 del usuario pedro22"),
            _op(3, "ya te proceso el retiro estimado"),
            _op(8, "", media="image")]
    assert motivo_de_agente(msgs) == "retiro"


# --- la generalizacion a `info` ---------------------------------------------------------
def test_preguntar_por_la_comision_es_info():
    """El hueco mas grande (1.383 sesiones, 2,0%) y el manual le dedica cuatro secciones."""
    msgs = [_cli(0, "cuanto me quedo de comision este mes?"),
            _op(1, "tu porcentaje base es el 25% estimado")]
    assert motivo_de_agente(msgs) == "info"


def test_preguntar_por_un_diseño_es_info():
    msgs = [_cli(0, "me pueden hacer un flyer para mi agencia?"),
            _op(1, "claro, te lo paso al area de diseño")]
    assert motivo_de_agente(msgs) == "info"


def test_querer_ser_agente_es_info():
    """Antes no tenia motivo NINGUNO: es el caso `0ac9b02c`, donde los 8 modelos
    discrepaban porque ninguna opcion era correcta."""
    msgs = [_cli(0, "hola, quiero ser agente de sorti como hago?"),
            _op(1, "te cuento como funciona el modelo")]
    assert motivo_de_agente(msgs) == "info"


def test_preguntar_por_la_clave_del_back_office_es_info():
    msgs = [_cli(0, "no puedo entrar al back office, como cambio mi clave?"),
            _op(1, "te ayudo con eso")]
    assert motivo_de_agente(msgs) == "info"


# --- lo que NO se clasifica -------------------------------------------------------------
def test_sin_pedido_ni_pregunta_no_hay_motivo():
    """None = se lo queda `agilidad`. 12% de las sesiones de agente no tienen ninguna
    señal, y no se les inventa un motivo para que la fila se vea completa."""
    msgs = [_cli(0, "buenas"), _op(1, "buenas, a la orden")]
    assert motivo_de_agente(msgs) is None


def test_sin_mensajes_no_rompe():
    assert motivo_de_agente([]) is None


def test_nunca_devuelve_problema():
    """`problema` no entra: no hay señal determinista de reclamo en el repo. Que este test
    exista es el recordatorio de que es una AUSENCIA decidida, no un olvido."""
    msgs = [_cli(0, "me quiero quejar, el mes pasado me pagaron mal la comision"),
            _op(1, "lo reviso estimado")]
    # cae en `info` (pregunta por la comision) y NO en problema
    assert motivo_de_agente(msgs) != "problema"


# --- precedencia ------------------------------------------------------------------------
def test_con_recarga_y_retiro_manda_la_recarga():
    """14 de 400 sesiones de agente tienen las dos transacciones. La precedencia espeja el
    guard del camino del jugador (src/scorer.py), donde el comprobante del cliente ancla
    `deposito`."""
    msgs = [_cli(0, "cargame 50 al usuario juan01"),
            _cli(1, "", media="image"),
            _op(2, "listo, acreditado"),
            _cli(30, "ahora necesito el pago de 200 de pedro22"),
            _op(33, "ya te proceso el retiro"),
            _op(35, "", media="image")]
    assert motivo_de_agente(msgs) == "deposito"


def test_la_transaccion_le_gana_a_la_pregunta():
    """Si hubo plata movida, el motivo es la transaccion: la pregunta suelta no la tapa."""
    msgs = [_cli(0, "cuanto es mi comision? y cargame 50 al usuario juan01"),
            _cli(1, "", media="image"),
            _op(2, "listo, acreditado")]
    assert motivo_de_agente(msgs) == "deposito"
