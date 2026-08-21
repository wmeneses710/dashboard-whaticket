"""`redireccion` deja de ser un SKIP y pasa a ser un MOTIVO. Decision del negocio (2026-08-20).

POR QUE CAMBIA. Desde el 2026-08-07 el traspaso puro era un skip condicionado: si el operador
solo mandaba "escribinos al <numero>" y ese numero era una linea NUESTRA y estaba CONNECTED,
la sesion se salteaba. El razonamiento era correcto -- ponerle 2 estrellas castiga al operador
por una migracion que decidio el negocio -- pero la consecuencia es que el traspaso desaparece
del tablero. No se puede contar, no se puede comparar entre operadores, y un supervisor no
puede ver cuantos clientes se estan mandando a otra linea.

MOTIVO Y RUBRICA VAN JUNTOS, Y NO ES OPCIONAL. Si `redireccion` entra en `MOTIVOS` sin una
rubrica determinista, cae al camino generico del LLM -- que es EXACTAMENTE el que le ponia
2 estrellas ("no atendio el motivo") y contra el que el skip protegia. Convertirlo en motivo
sin la rubrica no es un paso intermedio: es la regresion que el skip evitaba. El test
`test_la_linea_viva_no_es_una_falla` es el que lo ata.

EL EJE DE LA NOTA SALE DEL MANUAL DE ATC, no de la data:
  E07  "Transferir un chat sin notificar al cliente. El cliente debe saber que otro
        operador continuara su atencion para evitar confusiones."
  B09  "Informar al cliente cuando su caso sera transferido."
En un traspaso puro el aviso EXISTE por construccion (el mensaje de traspaso ES el aviso),
asi que B09 se cumple. Lo que discrimina es a DONDE lo mandan: a una linea viva el cliente
sigue atendido; sin numero o a una linea DISCONNECTED queda a la deriva, y eso si es mal
servicio. Ese eje ya estaba en el codigo (`traspaso_a_linea_viva`), solo no daba nota.

NO ES 5 ESTRELLAS. El operador cumplio una decision del negocio, pero el cliente todavia
tiene que escribir a otro lado: no se resolvio nada para el. Techo en 'buena'.
"""
from datetime import datetime, timedelta, timezone

from src.redireccion import respuesta_fue_solo_traspaso
from src.redireccion import score_redireccion
from src.rubrics import MOTIVO_RUBRICS, MOTIVOS
from src.sessions import evaluate_session

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
TRASPASO = "a partir de ahora te vamos a atender en el 0991194168"
VIVA = {"991194168": "CONNECTED"}
MUERTA = {"991194168": "DISCONNECTED"}


def _cli(minutos, body):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": None}


def _op(minutos, body):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "WEB"}


def _pedido_y_traspaso():
    """Bucket C: el cliente pidio algo concreto y el traspaso fue TODA la respuesta."""
    return [_cli(0, "buenas quiero hacer un retiro de 50"), _op(1, TRASPASO)]


# --- el motivo existe y tiene rubrica -------------------------------------------------
def test_redireccion_es_un_motivo():
    assert "redireccion" in MOTIVOS


def test_redireccion_tiene_rubrica():
    """Sin rubrica el motivo cae al camino generico del LLM, que es la regresion."""
    assert "redireccion" in MOTIVO_RUBRICS


# --- ya no se saltea ------------------------------------------------------------------
def test_el_traspaso_puro_ya_no_se_saltea():
    stats, rubric, eval_status, skip_reason = evaluate_session(
        _pedido_y_traspaso(), lineas=VIVA)
    assert eval_status == "evaluated"
    assert skip_reason is None


def test_sin_motivo_sigue_ganandole_al_traspaso():
    """Bucket A: si el cliente tampoco planteo nada, la etiqueta sigue siendo `sin_motivo`.
    Decision del negocio del 2026-08-07 que este cambio NO toca."""
    msgs = [_cli(0, "Hola"), _op(1, TRASPASO)]
    _s, _r, eval_status, skip_reason = evaluate_session(msgs, lineas=VIVA)
    assert (eval_status, skip_reason) == ("skipped", "sin_motivo")


# --- el predicado ---------------------------------------------------------------------
def test_solo_traspaso_cuando_toda_la_respuesta_es_traspaso():
    assert respuesta_fue_solo_traspaso(_pedido_y_traspaso()) is True


def test_no_es_solo_traspaso_si_el_operador_ademas_atendio():
    """Bucket B: el traspaso es UN mensaje dentro de una conversacion real -> manda el
    motivo real y `redireccion` no se mete."""
    msgs = [_cli(0, "quiero retirar"), _op(1, "ya te proceso el retiro estimado"),
            _op(2, TRASPASO)]
    assert respuesta_fue_solo_traspaso(msgs) is False


def test_sin_respuesta_del_negocio_no_es_traspaso():
    assert respuesta_fue_solo_traspaso([_cli(0, "hola?")]) is False


# --- la nota --------------------------------------------------------------------------
def test_la_linea_viva_no_es_una_falla():
    """EL TEST QUE IMPIDE LA REGRESION. El operador cumplio la migracion que decidio el
    negocio y el cliente sigue atendido: no puede caer en 2 estrellas como antes."""
    r = score_redireccion(_pedido_y_traspaso(), VIVA)
    assert r is not None
    assert r.motivo == "redireccion"
    assert r.stars == 4
    assert r.rating_label == "buena"


def test_la_linea_muerta_deja_al_cliente_a_la_deriva():
    r = score_redireccion(_pedido_y_traspaso(), MUERTA)
    assert r is not None
    assert r.stars == 2
    assert r.rating_label == "deficiente"


def test_un_traspaso_sin_numero_tambien_deja_a_la_deriva():
    msgs = [_cli(0, "quiero retirar"),
            _op(1, "a partir de ahora te vamos a atender por otro canal")]
    r = score_redireccion(msgs, VIVA)
    assert r is not None
    assert r.stars == 2


def test_no_da_nota_cuando_no_es_traspaso_puro():
    """La rubrica cede el turno igual que las otras deterministas: devuelve None."""
    msgs = [_cli(0, "quiero retirar"), _op(1, "ya te proceso el retiro estimado")]
    assert score_redireccion(msgs, VIVA) is None


def test_la_nota_dice_a_donde_lo_mandaron():
    """El rationale tiene que ser auditable: un supervisor tiene que poder ver por que."""
    r = score_redireccion(_pedido_y_traspaso(), VIVA)
    assert "linea" in r.rating_rationale.lower() or "línea" in r.rating_rationale.lower()
    assert r.dimensions.get("destino_utilizable") is True


# --- FALLAR DEL LADO SEGURO -----------------------------------------------------------
# MEDIDO el 2026-08-20 sobre las 839 sesiones cuya respuesta fue solo traspaso: la primera
# version de la rubrica daba 2 estrellas a 273, y **270 eran falsas acusaciones**:
#     238  el numero NO esta en `connections` -- pero `connections.number` viene NULL en
#          casi todas las filas, asi que el mapa solo conoce 9 lineas. Tres tails
#          concentran 230 de esas 238 (999303548, 983958331, 999303732): son lineas
#          reales del negocio que no estan registradas.
#      32  el destino es un LINK de WhatsApp ("escribenos al siguiente enlace:
#          https://wa.link/..."), que es un traspaso perfectamente valido.
#       3  el numero es NUESTRO y esta DISCONNECTED -- el unico caso de deriva real.
# Es el mismo criterio que el modulo ya aplicaba al skip ("sin el mapa de lineas no se
# skipea NADA, falla del lado seguro"): no se puede acusar con un mapa incompleto.
def test_un_numero_que_no_conocemos_no_es_una_acusacion():
    """No poder confirmar que la linea esta viva NO es prueba de que este muerta."""
    msgs = [_cli(0, "quiero retirar"),
            _op(1, "a partir de ahora te vamos a atender en el 0999303548")]
    r = score_redireccion(msgs, VIVA)   # 999303548 no esta en el mapa
    assert r.stars == 4, "un numero desconocido no puede costar 2 estrellas"


def test_un_link_de_whatsapp_es_un_destino_valido():
    msgs = [_cli(0, "info de agentes"),
            _op(1, "Si deseas mas información del programa de Agentes, escríbenos al "
                   "siguiente enlace: https://wa.link/l0ptr4")]
    r = score_redireccion(msgs, VIVA)
    assert r.stars == 4


def test_solo_acusa_cuando_la_linea_es_NUESTRA_y_esta_caida():
    """El unico caso probado de deriva: sabemos que es nuestra linea Y que esta caida."""
    msgs = [_cli(0, "quiero retirar"),
            _op(1, "contactate con esta línea: 0991194168")]
    r = score_redireccion(msgs, MUERTA)
    assert r.stars == 2
