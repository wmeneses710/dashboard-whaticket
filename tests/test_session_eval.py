"""Tests de la capa STATS + ELEGIBILIDAD a grano SESION (pieza 2 del diseno,
docs/diseno-evaluacion-unificada.md seccion 6).

El helper PURO evaluate_session corre message_stats + decide_rubric +
decide_eligibility sobre el transcript MERGEADO de la sesion (todos los episodios,
orden cronologico global). Es lo que mata los "skips fabricados": si el agente
respondio en un episodio hermano, al mergear la sesion tiene agent_message_count>0
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

def test_episodio_cliente_solo_aislado_seria_skip_no_agent_reply():
    # Episodio 1 SOLO-CLIENTE evaluado en soledad: se saltea (no_agent_reply).
    # Este es el skip que HOY se fabrica al evaluar por conversacion.
    ep1 = [_msg(False, "hola, estan?"), _msg(False, "sigo esperando")]
    stats, rubric, eval_status, skip_reason = evaluate_session(ep1)
    assert stats.agent_message_count == 0
    assert (eval_status, skip_reason) == ("skipped", "no_agent_reply")


def test_sesion_mergeada_absorbe_el_skip_fabricado():
    # CLAVE de la pieza: la MISMA sesion tiene ep1 solo-cliente + ep2 con respuesta
    # del agente. Mergeada -> agent_message_count>0 -> se EVALUA (no skip fabricado).
    ep1 = [_msg(False, "hola, estan?"), _msg(False, "sigo esperando")]
    ep2 = [_msg(False, "buenas, retomo"),
           _msg(True, "hola! si, contame", user_id="op1")]
    merged = ep1 + ep2  # orden cronologico global
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.agent_message_count > 0
    assert (eval_status, skip_reason) == ("evaluated", None)
    assert rubric == "human"


def test_sesion_genuinamente_sin_agente_sigue_skipped():
    # Todos los episodios solo-cliente: NO hay agente en NINGUNO -> sigue skipped
    # no_agent_reply (el merge no inventa una respuesta que no existe).
    ep1 = [_msg(False, "hola")]
    ep2 = [_msg(False, "alguien?"), _msg(False, "?")]
    merged = ep1 + ep2
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.agent_message_count == 0
    assert (eval_status, skip_reason) == ("skipped", "no_agent_reply")


def test_sesion_solo_bot_es_rubric_bot():
    # Negocio 100% bot en la sesion -> rubrica bot (mismo criterio que por conversacion).
    merged = [_msg(False, "hola"), _msg(True, "soy un bot", sent_from=BOT)]
    stats, rubric, eval_status, skip_reason = evaluate_session(merged)
    assert stats.agent_message_count == 0
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
    # rows en el shape del SELECT (from_me, is_note, body, sent_from, user_id, media_type),
    # provenientes de DOS conversaciones distintas de la misma sesion (merge).
    rows = [
        (False, False, "hola de c1", None, None, None),
        (True, False, "respuesta", None, "op1", None),
        (False, False, "sigo en c2", None, None, "image"),
    ]
    cur = _FakeCursor(rows=rows)
    out = context.fetch_session_messages(cur, "sess-1")

    # shape: mismos keys que context.fetch_messages
    assert out == [
        {"from_me": False, "is_note": False, "body": "hola de c1",
         "sent_from": None, "user_id": None, "media_type": None},
        {"from_me": True, "is_note": False, "body": "respuesta",
         "sent_from": None, "user_id": "op1", "media_type": None},
        {"from_me": False, "is_note": False, "body": "sigo en c2",
         "sent_from": None, "user_id": None, "media_type": "image"},
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
