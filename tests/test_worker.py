"""Tests del worker: lo verificable sin DB/LLM es la seleccion de pendientes."""
import src.worker as worker
from src.worker import (
    fetch_pending,
    fetch_pending_sessions,
    score_session_and_store,
    score_sessions_batch,
)


class _FakeCursor:
    def __init__(self, rows=(), description=()):
        self._rows = rows
        self.description = [type("C", (), {"name": n})() for n in description]
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows


# Cursor + conn con soporte de context manager (score_*_and_store usa `with
# conn.cursor() as cur`). El fetch de mensajes se monkeypatchea, asi que el cursor
# solo captura los execute del upsert.
class _CtxCursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return []


class _CtxConn:
    def __init__(self):
        self.commits = 0
        self.cursors = []

    def cursor(self):
        c = _CtxCursor()
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_fetch_pending_filtra_por_cuenta_y_excluye_ya_scoreadas():
    cur = _FakeCursor([], description=[])
    fetch_pending(cur, "datos", 20)
    query, params = cur.executed[0]
    assert "c.account = %(account)s" in query
    assert "NOT EXISTS" in query          # no re-scorea lo ya guardado
    assert params == {"account": "datos", "limit": 20}


def test_fetch_pending_devuelve_dicts():
    cur = _FakeCursor([("id1", "datos")], description=["id", "account"])
    assert fetch_pending(cur, "datos", 5) == [{"id": "id1", "account": "datos"}]


# --- PASO 2: scoring por SESION (aditivo, no toca el path por-conversacion) ---


def test_fetch_pending_sessions_arma_el_sql_del_gate_join_y_scoping():
    cur = _FakeCursor([], description=[])
    fetch_pending_sessions(cur, "datos", 30)
    query, params = cur.executed[0]
    # DECISION A: solo sesiones CERRADAS (end_at con margen de 6h).
    assert "interval '6 hours'" in query
    assert "end_at" in query
    # no re-scorea una sesion ya guardada (NOT EXISTS por session_id)...
    assert "NOT EXISTS" in query
    assert "s.session_id" in query
    # ...salvo que la sesion haya CRECIDO desde el score (continuacion diferida): el
    # re-open compara scored_at >= end_at para no quedar con nota vieja.
    assert "s.scored_at >= cs.end_at" in query
    # JOIN de la conversacion de ENTRADA por c.id = session_id.
    assert "c.id" in query and "session_id" in query
    # scopeado por cuenta + LIMIT parametrizado.
    assert "%(account)s" in query
    assert params == {"account": "datos", "limit": 30}


def test_fetch_pending_sessions_devuelve_dicts_con_session_id():
    cur = _FakeCursor([("id1", "datos", "id1")], description=["id", "account", "session_id"])
    assert fetch_pending_sessions(cur, "datos", 5) == [
        {"id": "id1", "account": "datos", "session_id": "id1"}
    ]


def _evaluated_session_messages():
    """Transcript minimo que decide_eligibility marca como 'evaluated'."""
    return [
        {"from_me": False, "is_note": False, "body": "hola", "sent_from": None,
         "user_id": None, "media_type": None},
        {"from_me": True, "is_note": False, "body": "buenas, te ayudo", "sent_from": "OP",
         "user_id": "op1", "media_type": None},
    ]


def _fake_score():
    from src.scorer import ScoreResult
    return ScoreResult(
        rubric="human", dimensions={"d": "x"}, rating_label="bueno",
        rating_rationale="ok", stars=4, llm_model="fake",
        atencion="empujo", deposit_observed=False,
    )


def _session_row(session_id="sess1"):
    """Fila devuelta por fetch_pending_sessions: conv de ENTRADA + session_id.

    id == session_id (el JOIN es c.id = conversation_sessions.session_id).
    """
    return {
        "id": session_id, "account": "datos", "ticket_id": "t1", "user_id": None,
        "created_at": None, "first_sent_message_at": None, "resolved_at": None,
        "queue_name": None, "channel": None, "session_id": session_id,
    }


def _params_of_upsert(conn):
    for c in conn.cursors:
        for query, params in c.executed:
            if "INSERT INTO conversation_scores" in query:
                return params
    return None


def test_score_session_and_store_evaluated_persiste_con_session_id(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _evaluated_session_messages())
    monkeypatch.setattr(worker, "score_conversation", lambda **kw: _fake_score())
    conn = _CtxConn()
    sess = _session_row("sess1")
    eval_status, skip_reason, score = score_session_and_store(conn, sess, llm=None, op_map={})
    assert eval_status == "evaluated" and skip_reason is None and score is not None
    assert conn.commits == 1
    params = _params_of_upsert(conn)
    assert params is not None
    # la fila queda keyeada por conversation_id = session_id, con la columna seteada.
    assert params["conversation_id"] == "sess1"
    assert params["session_id"] == "sess1"
    assert params["eval_status"] == "evaluated"


def test_score_session_and_store_evaluated_corre_el_llm_por_sesion(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _evaluated_session_messages())
    seen = {}

    def spy_score(**kw):
        seen["target"] = kw["target_messages"]
        seen["ctx"] = kw["thread_context"]
        return _fake_score()

    monkeypatch.setattr(worker, "score_conversation", spy_score)
    score_session_and_store(_CtxConn(), _session_row(), llm=None, op_map={})
    # scorea el transcript MERGEADO de la sesion, sin contexto de hilo por-conversacion.
    assert len(seen["target"]) == 2
    assert seen["ctx"] == ""


def test_score_session_and_store_skipped_no_scorea(monkeypatch):
    # Solo un mensaje del cliente -> no_agent_reply -> skipped, sin LLM ni stars.
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"from_me": False, "is_note": False, "body": "hola", "sent_from": None,
         "user_id": None, "media_type": None},
    ])

    def boom(**kw):
        raise AssertionError("no debe correr el LLM en una sesion skipped")

    monkeypatch.setattr(worker, "score_conversation", boom)
    conn = _CtxConn()
    eval_status, skip_reason, score = score_session_and_store(
        conn, _session_row(), llm=None, op_map={})
    assert eval_status == "skipped" and skip_reason == "no_agent_reply" and score is None
    params = _params_of_upsert(conn)
    assert params["session_id"] == "sess1" and params["stars"] is None


def test_score_sessions_batch_cuenta_y_no_aborta_por_una_excepcion(monkeypatch):
    sessions = [_session_row("s1"), _session_row("s2"), _session_row("s3")]
    monkeypatch.setattr(worker, "fetch_pending_sessions",
                        lambda cur, account, limit: sessions)

    def fake_score(conn, sess, llm, op_map):
        if sess["session_id"] == "s2":
            raise RuntimeError("boom")  # una sesion falla, el lote sigue
        return ("evaluated" if sess["session_id"] == "s1" else "skipped", None, None)

    monkeypatch.setattr(worker, "score_session_and_store", fake_score)
    counts = score_sessions_batch(_CtxConn(), llm=None, account="datos", limit=10, op_map={})
    assert counts == {"evaluated": 1, "skipped": 1, "error": 1, "seen": 3}
