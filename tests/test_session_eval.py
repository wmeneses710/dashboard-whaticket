"""Tests de la capa STATS + ELEGIBILIDAD a grano SESION (pieza 2 del diseno,
docs/diseno-evaluacion-unificada.md seccion 6).

El helper PURO evaluate_session corre message_stats + decide_rubric +
decide_eligibility sobre el transcript MERGEADO de la sesion (todos los episodios,
orden cronologico global). Es lo que mata los "skips fabricados": si el agente
respondio en un episodio hermano, al mergear la sesion tiene operator_message_count>0
y se evalua en vez de saltear falso no_agent_reply.

Lo puro se valida con datos en memoria (sin BD). Para fetch_session_messages se usa
un cursor falso (NO ejecuta SQL): valida ESTRUCTURA del query (join por session_id,
ORDER BY con tiebreaker determinista) y el shape del dict. El cursor falso no puede
detectar errores de SQL ni el orden cronologico real (lo hace el ORDER BY en la BD);
eso se valida corriendo contra la copia real whaticket_copia (gotcha del proyecto).
"""
import src.context as context
from src.metrics import primary_operator
from src.sessions import evaluate_session

BOT = "CHATBOT"


def _msg(from_me, body="hola", *, user_id=None, sent_from=None,
         is_note=False, media_type=None):
    return {"from_me": from_me, "is_note": is_note, "body": body,
            "sent_from": sent_from, "user_id": user_id, "media_type": media_type}


# --- evaluate_session: el skip fabricado desaparece --------------------------

def test_episodio_cliente_solo_aislado_ya_no_se_saltea():
    """Lo que este test protege sigue siendo lo importante: que evaluar el episodio EN
    SOLEDAD ve cero mensajes del operador. Eso es el skip FABRICADO que la sesionizacion
    vino a matar, y `test_sesion_mergeada_absorbe_el_skip_fabricado` lo demuestra al lado.
    Lo que cambio el 2026-08-21 es que "nadie respondio" ya no se saltea: se evalua y lleva
    1 estrella (src/sin_respuesta.py)."""
    ep1 = [_msg(False, "hola, estan?"), _msg(False, "sigo esperando")]
    stats, rubric, eval_status, skip_reason = evaluate_session(ep1)
    assert stats.operator_message_count == 0
    assert (eval_status, skip_reason) == ("evaluated", None)


def test_sesion_mergeada_absorbe_el_skip_fabricado():
    # CLAVE de la pieza: la MISMA sesion tiene ep1 solo-cliente + ep2 con respuesta
    # del agente. Mergeada -> operator_message_count>0 -> se EVALUA (no skip fabricado).
    ep1 = [_msg(False, "hola, estan?"), _msg(False, "sigo esperando")]
    ep2 = [_msg(False, "buenas, retomo"),
           _msg(True, "hola! si, contame", user_id="op1")]
    merged = ep1 + ep2  # orden cronologico global
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.operator_message_count > 0
    assert (eval_status, skip_reason) == ("evaluated", None)
    assert rubric == "human"


def test_el_merge_no_inventa_una_respuesta_que_no_existe():
    """EL INVARIANTE NO CAMBIO, cambio su consecuencia. Todos los episodios solo-cliente ->
    el merge NO fabrica un operador: `operator_message_count` sigue en cero. Lo que cambio
    el 2026-08-21 es que eso ya no se saltea -- se evalua y lleva 1 estrella."""
    ep1 = [_msg(False, "hola")]
    ep2 = [_msg(False, "alguien?"), _msg(False, "?")]
    merged = ep1 + ep2
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.operator_message_count == 0
    assert (eval_status, skip_reason) == ("evaluated", None)


def test_sesion_solo_bot_es_rubric_bot():
    # Negocio 100% bot en la sesion -> rubrica bot (mismo criterio que por conversacion).
    # El cliente plantea algo: si solo dijera "hola" el skip `sin_motivo` ganaria
    # antes de llegar a la rubrica, y este test es sobre la rubrica.
    merged = [_msg(False, "como recargo?"), _msg(True, "soy un bot", sent_from=BOT)]
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.operator_message_count == 0
    assert stats.bot_message_count == 1
    assert rubric == "bot"
    assert eval_status == "evaluated"


def test_sesion_bot_saluda_humano_atiende_es_human():
    # Mixto bot+humano en la sesion mergeada -> human (la calidad la puso la persona).
    merged = [_msg(True, "hola, soy el asistente", sent_from=BOT),
              _msg(False, "quiero recargar"),
              _msg(True, "te ayudo con eso", user_id="op1")]
    _, rubric, eval_status, _ = evaluate_session(merged)
    assert rubric == "human"
    assert eval_status == "evaluated"


def test_sesion_sin_texto_del_cliente_se_saltea_media_only():
    # Cliente solo mando media (sin texto legible) -> skipped customer_media_only,
    # ordenado ANTES del chequeo de no_agent_reply.
    merged = [_msg(False, "", media_type="image"),
              _msg(True, "recibido", user_id="op1")]
    _, _, eval_status, skip_reason = evaluate_session(merged)
    assert (eval_status, skip_reason) == ("skipped", "customer_media_only")


def test_sesion_solo_notas_internas_se_saltea():
    merged = [_msg(True, "nota interna", is_note=True, user_id="op1")]
    _, _, eval_status, skip_reason = evaluate_session(merged)
    assert (eval_status, skip_reason) == ("skipped", "internal_notes_only")


# --- primary_operator sobre la sesion mergeada -------------------------------

def test_primary_operator_sobre_sesion_mergeada():
    # op1 responde en ep1, op1 y op2 en ep2. Sobre la sesion completa op1 domina.
    ep1 = [_msg(False, "hola"), _msg(True, "hola", user_id="op1")]
    ep2 = [_msg(True, "seguimos", user_id="op1"),
           _msg(True, "yo tambien ayudo", user_id="op2")]
    merged = ep1 + ep2
    assert primary_operator(merged) == "op1"


def test_primary_operator_solo_bot_es_none():
    merged = [_msg(False, "hola"), _msg(True, "bot", sent_from=BOT, user_id="botid")]
    assert primary_operator(merged) is None


# --- fetch_session_messages: cursor falso ------------------------------------

class _FakeCursor:
    """Cursor falso: guarda el query ejecutado y devuelve rows fijas en fetchall."""

    def __init__(self, rows=()):
        self.executed = []
        self._rows = rows

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows


def test_fetch_session_messages_shape_y_query():
    # rows en el shape del SELECT (created_at, from_me, is_note, body, sent_from,
    # user_id, media_type, ack), provenientes de DOS conversaciones distintas de la misma
    # sesion (merge). `created_at` se agrego porque la rubrica de agilidad lo necesita
    # en cada mensaje, y `ack` porque `cliente_abandono_tras_pedido` necesita saber si el
    # cliente LEYO el pedido (ver tests/test_context.py, contrato de forma).
    from datetime import datetime, timezone
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        (t0, False, False, "hola de c1", None, None, None, 0),
        (t0, True, False, "respuesta", None, "op1", None, 3),
        (t0, False, False, "sigo en c2", None, None, "image", 0),
    ]
    cur = _FakeCursor(rows=rows)
    out = context.fetch_session_messages(cur, "sess-1")

    # shape: mismos keys que context.fetch_messages, mas `ack`
    assert out == [
        {"created_at": t0, "from_me": False, "is_note": False, "body": "hola de c1",
         "sent_from": None, "user_id": None, "media_type": None, "ack": 0},
        {"created_at": t0, "from_me": True, "is_note": False, "body": "respuesta",
         "sent_from": None, "user_id": "op1", "media_type": None, "ack": 3},
        {"created_at": t0, "from_me": False, "is_note": False, "body": "sigo en c2",
         "sent_from": None, "user_id": None, "media_type": "image", "ack": 0},
    ]

    query, params = cur.executed[0]
    assert params == ("sess-1",)
    # join por el mapeo de sesion (grano episodio) filtrado por session_id
    assert "conversation_session_map" in query
    assert "session_id" in query
    # orden cronologico GLOBAL con tiebreaker determinista (leccion pieza 1)
    upper = query.upper()
    assert "ORDER BY" in upper
    order_clause = upper.split("ORDER BY", 1)[1]
    assert "CREATED_AT" in order_clause and ".ID" in order_clause


def test_fetch_session_messages_sesion_vacia():
    cur = _FakeCursor(rows=[])
    assert context.fetch_session_messages(cur, "sess-x") == []


# --- `sin motivo`: el cliente nunca planteo nada -----------------------------
# Motivo definido por el negocio el 2026-08-05 ("hola y se fue") y medido el
# 2026-08-06: 42 de 1.008 sesiones de jugador (4,2%), concentradas en `registro`
# (8,7%), que es donde vive la prospeccion saliente. NO se califica: ponerle nota al
# operador por un contacto en frio que no prendio es castigarlo por algo que no hizo.

def test_sin_motivo_se_saltea():
    # Todo lo que dijo el cliente es cortesia. Strings reales del dataset.
    for texto in ("Si", "Hola", "Ok", "Bueno", "Ok listo", "Bueno Ok Bueno",
                  "Hola Si Ya", "Gracias"):
        msgs = [_msg(True, "hola! te cuento que soy agente de Sorti365"),
                _msg(False, texto),
                _msg(True, "te creo la cuenta?")]
        _, _, status, reason = evaluate_session(msgs)
        assert (status, reason) == ("skipped", "sin_motivo"), texto


def test_una_sola_palabra_de_verdad_lo_vuelve_evaluable():
    # El detector falla del lado seguro: strings REALES que el primer intento
    # marcaba mal como sin-motivo y que son pedidos o preguntas de verdad.
    for texto in ("buenas mandeme una cuenta pichincha", "mas informacion por favor",
                  "de q de trata", "hola, hacen recarga de $1 o $2"):
        msgs = [_msg(True, "hola!"), _msg(False, texto), _msg(True, "claro que si")]
        _, _, status, _ = evaluate_session(msgs)
        assert status == "evaluated", texto


def test_un_comprobante_del_cliente_NO_es_sin_motivo():
    # Mandar el comprobante ES plantear algo, aunque el texto sea "Listo".
    msgs = [_msg(True, "mandame el comprobante"),
            _msg(False, "Listo", media_type="image"),
            _msg(True, "ya te lo acredito")]
    _, _, status, _ = evaluate_session(msgs)
    assert status == "evaluated"


def test_nadie_respondio_le_gana_a_sin_motivo():
    """El ORDEN se conserva y sigue importando: un "Hola" sin respuesta NO es `sin_motivo`.
    Antes eso se expresaba como "gana el skip de no_agent_reply"; desde el 2026-08-21 se
    expresa como "se evalua", porque nadie-respondio dejo de ser un skip. La conclusion es
    la misma y es la que vale: la sesion no desaparece etiquetada como que el cliente no
    planteo nada, cuando lo que paso es que no le contestaron.

    Lo hace un guard explicito en `evaluate_session` (`hubo_negocio`): antes la prioridad se
    cumplia sola porque `no_agent_reply` ganaba en `decide_eligibility`, y al dejar de ser un
    skip habia que escribirla."""
    msgs = [_msg(False, "Hola")]
    _, _, status, reason = evaluate_session(msgs)
    assert (status, reason) == ("evaluated", None)
