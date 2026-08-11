"""Tests del digest de contexto del hilo (parte pura, sin DB)."""
from datetime import datetime, timedelta, timezone

from src.context import MAX_THREAD_VISITS, format_thread_digest

BASE = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _visit(i, is_bot, msg):
    return {"created_at": BASE + timedelta(hours=i), "is_bot": is_bot, "first_customer_msg": msg}


def test_digest_vacio_si_no_hay_visitas():
    assert format_thread_digest([]) == ""


def test_digest_rotula_bot_y_agente_y_muestra_cliente():
    d = format_thread_digest([_visit(1, True, "hola"), _visit(2, False, "gracias")])
    assert "[BOT]" in d
    assert "[OPERADOR]" in d
    assert "hola" in d


def test_digest_capa_a_las_ultimas_n_visitas():
    visits = [_visit(i, False, f"m{i}") for i in range(30)]
    d = format_thread_digest(visits)
    lineas = [l for l in d.splitlines() if l.startswith("- ")]
    assert len(lineas) == MAX_THREAD_VISITS       # se cap
    assert "m29" in d                             # conserva las mas recientes
    assert "m0" not in d                          # descarta las viejas
    assert "omitidas" in d                        # marca lo omitido


def test_digest_sin_mensaje_de_cliente_se_marca():
    d = format_thread_digest([_visit(1, False, None)])
    assert "sin mensaje de cliente" in d


# --- CONTRATO de forma: fetch_session_messages vs lo que consume el scoring -------
# ESTE BLOQUE EXISTE POR UN BUG REAL. `src/agilidad.py` necesita `created_at` en cada
# mensaje y `fetch_session_messages` no lo devolvia: el KeyError aparecio recien en una
# tanda contra la BD (46 de 60 sesiones de agente reventaron). Los tests unitarios no lo
# vieron porque fabricaban los dicts A MANO, con created_at incluido. La leccion: hay que
# testear la forma REAL, no la conveniente.

class _CursorDeUnaFila:
    """Cursor falso que devuelve UNA fila con tantas columnas como pida el SELECT."""

    def __init__(self):
        self.query = ""

    def execute(self, q, p=None):
        self.query = q

    def fetchall(self):
        from datetime import datetime, timezone
        n = self.query.count("m.")  # cuantas columnas pide el SELECT
        valores = {
            "m.created_at": datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
            "m.from_me": False, "m.is_note": False, "m.body": "hola",
            "m.sent_from": None, "m.user_id": None, "m.media_type": None,
            "m.ack": 3,
        }
        # Reconstruye la fila en el orden en que aparecen en el SELECT.
        select = self.query.split("FROM")[0]
        cols = [c for c in valores if c in select]
        cols.sort(key=select.index)
        return [tuple(valores[c] for c in cols)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_session_messages_devuelve_created_at():
    from src.context import fetch_session_messages
    filas = fetch_session_messages(_CursorDeUnaFila(), "sess1")
    assert filas, "deberia devolver al menos una fila"
    assert "created_at" in filas[0], \
        "src/agilidad.py lo necesita; sin esto revienta con KeyError en produccion"


def test_fetch_session_messages_devuelve_ack():
    # MISMA LECCION que created_at, un año despues: `cliente_abandono_tras_pedido` usa
    # `ack` para saber si el cliente LEYO el pedido. Si el SELECT no lo trae, la señal se
    # degrada a True en silencio y el techo de `registro` sigue saltandose — sin reventar
    # ni un test unitario, porque los fixtures fabrican el dict a mano.
    from src.context import fetch_session_messages
    filas = fetch_session_messages(_CursorDeUnaFila(), "sess1")
    assert "ack" in filas[0], \
        "src/signals.cliente_abandono_tras_pedido lo necesita para no inventar abandonos"


def test_la_forma_real_alcanza_para_la_rubrica_de_agilidad():
    # El test que faltaba: consumir la salida REAL de fetch_session_messages con la
    # funcion REAL de agilidad. Si a una le falta una clave que la otra usa, revienta aca
    # y no en una tanda de 6 horas.
    from src.agilidad import calificar_agilidad
    from src.context import fetch_session_messages
    msgs = fetch_session_messages(_CursorDeUnaFila(), "sess1")
    calificar_agilidad(msgs)   # no debe lanzar


def test_la_forma_real_alcanza_para_TODAS_las_rubricas_por_motivo():
    # Mismo contrato que el de agilidad, extendido a las seis rubricas deterministas
    # que se sumaron el 2026-08-06. Todas consumen la salida de fetch_session_messages
    # y todas miden tiempos, asi que si a una le falta `created_at` (o cualquier otra
    # clave) revienta aca y no contra la BD. Ya paso una vez: la rubrica de deposito
    # tiro KeyError en 7 tests por exactamente esto.
    from src.context import fetch_session_messages
    from src.deposito import calificar_deposito
    from src.info import calificar_info
    from src.promo import calificar_promo
    from src.registro import calificar_registro
    from src.retiro import calificar_retiro
    from src.soporte import calificar_soporte

    msgs = fetch_session_messages(_CursorDeUnaFila(), "sess1")
    for calificar in (calificar_deposito, calificar_retiro, calificar_registro,
                      calificar_promo, calificar_soporte, calificar_info):
        calificar(msgs)   # no debe lanzar


def test_la_forma_real_alcanza_para_el_skip_sin_motivo():
    from src.context import fetch_session_messages
    from src.sessions import evaluate_session
    evaluate_session(fetch_session_messages(_CursorDeUnaFila(), "sess1"))


def test_la_forma_real_alcanza_para_las_stats_y_las_senales():
    from src.context import fetch_session_messages
    from src.metrics import message_stats, primary_operator
    from src.signals import operator_confirmation, operator_resolved
    msgs = fetch_session_messages(_CursorDeUnaFila(), "sess1")
    message_stats(msgs)
    primary_operator(msgs)
    operator_confirmation(msgs)
    operator_resolved(msgs)
