"""Techo del fall-through TRANSACCIONAL (deposito/retiro).

Llegar al pase con LLM con motivo `deposito` o `retiro` PRUEBA que su rubrica
determinista devolvio None, o sea que el gate no encontro la transaccion: en
`deposito` no hay comprobante del cliente que acreditar, en `retiro` no hay pedido
de plata que entregar.

En ese camino hay DOS poblaciones distintas y solo una es un problema:

  1. el cliente PREGUNTO (como recargo, cuanto tarda un retiro) y el operador
     contesto bien. Es el mejor escenario disponible de una consulta -> el 5 se
     conserva. Medido sobre el rescore v13: 79 de las 102 filas en 5 estrellas del
     fall-through de `deposito` son de esta clase.
  2. el operador AFIRMA que la plata se movio y no hay NADA que lo respalde. Son
     23 de esas 102. `deposito.es_transaccion` existe justo por esto: el
     comprobante "se exige por AUDITORIA". Una afirmacion sin evidencia no puede
     valer el mejor escenario del motivo, porque el mejor escenario de la rubrica
     es una acreditacion CONFIRMADA Y VERIFICABLE.

El mismo docstring de `deposito.es_transaccion` ya midio esta enfermedad para los
depositos CON comprobante que caian al pase con LLM (sacaban 5 estrellas el 68,2%
de las veces contra el 3,6% de las transacciones). Este techo cierra la mitad que
quedaba: la de los que no tienen comprobante en absoluto.

En `retiro` la evidencia es asimetrica: el comprobante lo manda el OPERADOR, asi
que su media ES la entrega y protege el 5.
"""
from datetime import datetime, timedelta, timezone

from src.scorer import score_by_motivo

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _cli(body: str, *, minute: int = 0, media: str | None = None) -> dict:
    return {"from_me": False, "is_note": False, "body": body, "media_type": media,
            "created_at": T0 + timedelta(minutes=minute), "sent_from": None}


def _op(body: str, *, minute: int = 1, media: str | None = None) -> dict:
    return {"from_me": True, "is_note": False, "body": body, "media_type": media,
            "created_at": T0 + timedelta(minutes=minute), "sent_from": "WEB"}


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(motivo: str) -> dict:
    """Hechos que derivan a 'excelente': atendio limpio + accion extra."""
    return {
        "motivo": motivo,
        "dimensions": {"resolucion": "ok", "iniciativa": "ok", "cortesia": "cordial",
                       "aciertos": [], "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": True,
        "cortesia_destacada": True,
        "hubo_maltrato_grave": False,
        "rating_rationale": "atendio bien",
        "recomendacion": "",
        "atencion": "empujo",
        "deposit_observed": False,
    }


def _score(msgs, motivo):
    return score_by_motivo(target_messages=msgs, thread_context="",
                           llm=FakeLLM(_resp(motivo)))


# --- deposito -------------------------------------------------------------

def test_deposito_que_afirma_la_acreditacion_sin_comprobante_no_llega_a_excelente():
    # El caso real del rescore v13: el cliente no adjunto nada y el operador igual
    # dice que el saldo entro. No hay comprobante que auditar -> no es el mejor
    # escenario del motivo.
    msgs = [
        _cli("Me ayuda recargando"),
        _op("¡Gracias por tu recarga, Vicente! Tu saldo ya está disponible."),
    ]
    r = _score(msgs, "deposito")
    assert r.rating_label == "buena" and r.stars == 4


def test_deposito_consulta_bien_atendida_conserva_el_excelente():
    # LA CONTRACARA, y es la que evita que el techo sea un exceso: sin afirmacion
    # de acreditacion el operador solo contesto una consulta, y contestarla bien es
    # el mejor escenario disponible.
    msgs = [
        _cli("Buenas, ¿cómo hago para recargar?"),
        _op("Podés hacerlo por transferencia o en cualquier agente, te paso los pasos."),
    ]
    r = _score(msgs, "deposito")
    assert r.rating_label == "excelente" and r.stars == 5


def test_es_un_TECHO_no_un_castigo():
    # Sin `hizo_accion_extra` la derivacion ya da 'buena': el techo no tiene que
    # bajarla mas. Baja el 5 a 4 y no toca nada por debajo.
    msgs = [
        _cli("Me ayuda recargando"),
        _op("¡Gracias por tu recarga! Tu saldo ya está disponible."),
    ]
    resp = _resp("deposito")
    resp["hizo_accion_extra"] = False
    resp["cortesia_destacada"] = False
    r = score_by_motivo(target_messages=msgs, thread_context="", llm=FakeLLM(resp))
    assert r.rating_label == "buena" and r.stars == 4


# --- retiro ---------------------------------------------------------------

def test_retiro_que_afirma_el_pago_sin_comprobante_no_llega_a_excelente():
    msgs = [
        _cli("¿Cuánto tarda un retiro?"),
        _op("Ya está acreditado en tu cuenta bancaria."),
    ]
    r = _score(msgs, "retiro")
    assert r.rating_label == "buena" and r.stars == 4


def test_retiro_con_el_comprobante_del_operador_conserva_el_excelente():
    # ASIMETRIA DEL MOTIVO: en retiro el comprobante lo manda el OPERADOR, asi que
    # su media es la entrega misma y respalda la afirmacion.
    msgs = [
        _cli("¿Cuánto tarda un retiro?"),
        _op("Ya está acreditado en tu cuenta bancaria.", media="image"),
    ]
    r = _score(msgs, "retiro")
    assert r.rating_label == "excelente" and r.stars == 5


# --- el techo NO se derrama a los motivos no transaccionales --------------

def test_el_techo_no_toca_los_motivos_que_no_son_transaccionales():
    # `problema` es el unico motivo sin rubrica determinista propia, asi que SIEMPRE
    # cae a este camino: sirve para probar que el techo mira el motivo y no se
    # derrama sobre todo el fall-through. Ahi "acreditado" no es una transaccion
    # que este rubrica deba auditar.
    msgs = [
        _cli("No me aparece el bono que reclamé"),
        _op("Ya está acreditado tu bono, mirá la sección de promociones."),
    ]
    r = _score(msgs, "problema")
    assert r.rating_label == "excelente" and r.stars == 5
