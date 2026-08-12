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


def test_conv_fields_incluye_is_new_contact():
    # necesario para detectar adquisicion (is_new_contact AND segmento jugador) en
    # build_score_record; sin esta columna la conversacion de ENTRADA no la trae.
    assert "c.is_new_contact" in worker._CONV_FIELDS


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
    """Transcript minimo que decide_eligibility marca como 'evaluated'.

    `created_at` viaja en CADA mensaje porque la consulta real siempre lo trae (el contrato
    lo fija tests/test_context.py) y los tiempos se derivan del transcript, no de los campos
    del CRM. Un fixture sin el no representa lo que ve produccion.
    """
    return [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "me ayudas con una recarga?", "sent_from": None,
         "user_id": None, "media_type": None},
        {"created_at": _T0 + timedelta(seconds=40), "from_me": True, "is_note": False,
         "body": "buenas, te ayudo", "sent_from": "OP",
         "user_id": "op1", "media_type": None},
    ]


def _fake_score():
    from src.scorer import ScoreResult
    return ScoreResult(
        rubric="deposito", dimensions={"d": "x"}, rating_label="buena",
        rating_rationale="ok", stars=4, llm_model="fake",
        atencion="empujo", deposit_observed=False, motivo="deposito",
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
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
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

    monkeypatch.setattr(worker, "score_by_motivo", spy_score)
    score_session_and_store(_CtxConn(), _session_row(), llm=None, op_map={})
    # scorea el transcript MERGEADO de la sesion, sin contexto de hilo por-conversacion.
    assert len(seen["target"]) == 2
    assert seen["ctx"] == ""


def test_score_session_and_store_skipped_no_scorea(monkeypatch):
    # Solo un mensaje del cliente -> no_agent_reply -> skipped, sin LLM ni stars.
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"from_me": False, "is_note": False, "body": "me ayudas con una recarga?", "sent_from": None,
         "user_id": None, "media_type": None},
    ])

    def boom(**kw):
        raise AssertionError("no debe correr el LLM en una sesion skipped")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
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

    def fake_score(conn, sess, llm, op_map, recommender=None, lineas=None):
        if sess["session_id"] == "s2":
            raise RuntimeError("boom")  # una sesion falla, el lote sigue
        return ("evaluated" if sess["session_id"] == "s1" else "skipped", None, None)

    monkeypatch.setattr(worker, "score_session_and_store", fake_score)
    counts = score_sessions_batch(_CtxConn(), llm=None, account="datos", limit=10, op_map={})
    assert counts == {"evaluated": 1, "skipped": 1, "error": 1, "seen": 3}


def test_run_worker_loop_no_scorea_si_otra_instancia_tiene_el_lock(monkeypatch):
    """Guard singleton: si pg_try_advisory_lock devuelve False, la instancia se retira
    sin scorear (evita el deadlock en conversation_sessions entre réplicas)."""
    import types
    import psycopg

    connects = []

    class _LockConn:
        autocommit = False

        def execute(self, *a, **k):
            return types.SimpleNamespace(fetchone=lambda: [False])  # lock NO adquirido

        def close(self):
            pass

    def fake_connect(*a, **k):
        connects.append(1)
        return _LockConn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    class _FakeLLM:
        def __init__(self, *a, **k):
            self.model = "qwen"
            self.calls = {"fast": 0, "fallback": 0, "empty": 0}

        def check_model(self):
            return (True, "ok")

    monkeypatch.setattr(worker, "OllamaClient", _FakeLLM)

    called = {"batch": 0}
    monkeypatch.setattr(worker, "score_sessions_batch",
                        lambda *a, **k: called.__setitem__("batch", called["batch"] + 1))

    cfg = types.SimpleNamespace(
        database_url="postgresql://x", ollama_url="http://x", ollama_model="qwen",
        ollama_token="", recom_subagent_enabled=False,
        scoring_accounts=("sistemas",), scoring_batch_size=20, scoring_poll_seconds=1,
        ollama_num_ctx=16384, ollama_num_predict=768, llm_fast_attempts=2,
    )
    worker.run_worker_loop(cfg, should_stop=lambda: True)

    assert called["batch"] == 0        # no scoreó: se retiró por el lock
    assert len(connects) == 1          # solo la conexión del lock, no llegó a migración/refresh


# --- segmento AGENTE: rating DETERMINISTA, sin LLM --------------------------------
# La agilidad se calcula con timestamps (src/agilidad.py). El pase con LLM NO debe
# correr para agente: la vara comercial del jugador no aplica a un revendedor.

def _agente_session_row():
    row = _session_row("sessA")
    row["queue_name"] = "Agente 👨👩"   # segment_for_queue -> "agente"
    return row


def _mensajes_de_agente(minutos_respuesta):
    """Agente pide una recarga a las 15:00 Ecuador y el operador responde N minutos despues."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)  # 15:00 Ecuador
    return [
        {"created_at": t0, "from_me": False, "is_note": False,
         "body": "Me ayuda con una recarga a mi agencia", "sent_from": None,
         "user_id": None, "media_type": None},
        {"created_at": t0 + timedelta(minutes=minutos_respuesta), "from_me": True,
         "is_note": False, "body": "Tu saldo ya esta disponible", "sent_from": "OP",
         "user_id": "op1", "media_type": None},
    ]


def test_sesion_de_agente_NO_llama_al_llm(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _mensajes_de_agente(1))

    def boom(**kw):
        raise AssertionError("el LLM no debe correr en el segmento agente")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    status, _, score = score_session_and_store(conn, _agente_session_row(),
                                               llm=None, op_map={})
    assert status == "evaluated"
    assert score is not None and score.stars == 5
    params = _params_of_upsert(conn)
    assert params["stars"] == 5
    assert params["llm_model"] == "determinista/agilidad-v1"


def test_sesion_de_agente_lenta_baja_la_nota(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _mensajes_de_agente(40))
    monkeypatch.setattr(worker, "score_by_motivo",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("sin LLM")))
    _, _, score = score_session_and_store(_CtxConn(), _agente_session_row(),
                                          llm=None, op_map={})
    assert score.stars == 2


def test_sesion_de_JUGADOR_sigue_usando_el_llm(monkeypatch):
    # Contraprueba: el path determinista es SOLO para agente.
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _evaluated_session_messages())
    llamo = {"si": False}

    def spy(**kw):
        llamo["si"] = True
        return _fake_score()

    monkeypatch.setattr(worker, "score_by_motivo", spy)
    row = _session_row("sessJ")
    row["queue_name"] = "Jugadores"
    score_session_and_store(_CtxConn(), row, llm=None, op_map={})
    assert llamo["si"] is True


def test_sesion_de_agente_sin_pedidos_medibles_queda_sin_nota(monkeypatch):
    # Solo cortesias: no hay agilidad que medir. NO se inventa una nota media, y
    # tampoco se cae al LLM (seguiria aplicando la vara del jugador).
    #
    # OJO: el agente tiene que PEDIR algo para llegar hasta aca. Si su unico mensaje
    # fuera "Gracias", el skip `sin_motivo` gana antes (y esta bien: nadie planteo
    # nada). Lo que este test fija es el caso distinto — hubo un pedido de verdad,
    # pero cae FUERA del horario de operacion, asi que no hay agilidad medible.
    from datetime import datetime, timezone
    t0 = datetime(2026, 3, 10, 7, 0, 0, tzinfo=timezone.utc)   # 02:00 Ecuador
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"created_at": t0, "from_me": False, "is_note": False,
         "body": "me cargas 30 a la agencia?", "sent_from": None, "user_id": None,
         "media_type": "chat"},
        {"created_at": t0, "from_me": True, "is_note": False, "body": "a la orden",
         "sent_from": "OP", "user_id": "op1", "media_type": "chat"},
    ])
    monkeypatch.setattr(worker, "score_by_motivo",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("sin LLM")))
    conn = _CtxConn()
    status, _, score = score_session_and_store(conn, _agente_session_row(),
                                               llm=None, op_map={})
    assert status == "evaluated"       # la sesion es evaluable; solo no tiene nota
    assert score is None
    assert _params_of_upsert(conn)["stars"] is None


# --- redireccion: el skip necesita el mapa de lineas cableado hasta aca -----------
# Sin este cableado la regla existe en src/redireccion.py pero NUNCA se aplica en
# produccion, que es la unica parte que importa.

def _redireccion_session_messages():
    # `created_at` en cada mensaje: la consulta real siempre lo trae (contrato en
    # tests/test_context.py) y los tiempos se derivan del transcript.
    return [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "Buenas para recargar 5",
         "sent_from": None, "user_id": None, "media_type": "chat"},
        {"created_at": _T0 + timedelta(seconds=25), "from_me": True, "is_note": False,
         "media_type": "chat", "user_id": "u1", "sent_from": "OPERATOR",
         "body": "A partir de ahora te estaremos atendiendo desde el 0991194133"},
    ]


def test_score_session_and_store_saltea_por_redireccion(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _redireccion_session_messages())

    def boom(**kw):
        raise AssertionError("no debe correr el LLM en una redireccion skipeada")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    eval_status, skip_reason, score = score_session_and_store(
        conn, _session_row(), llm=None, op_map={}, lineas={"991194133": "CONNECTED"})
    assert (eval_status, skip_reason) == ("skipped", "redireccion")
    assert score is None
    assert _params_of_upsert(conn)["stars"] is None


def test_sin_mapa_de_lineas_la_redireccion_se_evalua_igual(monkeypatch):
    # Falla del lado seguro: si el mapa no llego, no se regala el skip.
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _redireccion_session_messages())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    eval_status, _, _ = score_session_and_store(
        _CtxConn(), _session_row(), llm=None, op_map={})
    assert eval_status == "evaluated"


def test_score_sessions_batch_construye_el_mapa_de_lineas_si_no_lo_recibe(monkeypatch):
    vistos = {}
    monkeypatch.setattr(worker, "fetch_pending_sessions", lambda cur, a, l: [_session_row()])
    monkeypatch.setattr(worker, "build_operator_map", lambda cur: {})
    monkeypatch.setattr(worker, "build_lineas_map", lambda cur: {"991194133": "CONNECTED"})

    def spy(conn, sess, llm, op_map, recommender=None, lineas=None):
        vistos["lineas"] = lineas
        return "skipped", "redireccion", None

    monkeypatch.setattr(worker, "score_session_and_store", spy)
    score_sessions_batch(_CtxConn(), llm=None, account="datos", limit=10)
    assert vistos["lineas"] == {"991194133": "CONNECTED"}


# --- LOS TIEMPOS Y EL OPERADOR SE ACOTAN A LA INTERACCION JUZGADA -------------------
# MEDIDO el 2026-08-12 sobre el rescore v5: los campos del CRM describen el ENVASE. En el
# caso `f9b31f4f` (17 interacciones) `created_at` sale de la primera, `first_sent_message_at`
# de la segunda (51,5 h despues) y `resolved_at` de la ultima. Y peor: en 152 de 585 sesiones
# multi-interaccion de deposito/retiro (26,0%) la nota se le cargaba a un operador que ni
# aparece en la interaccion juzgada -- 25 de ellas con 1 o 2 estrellas.
# Solo aplica a `deposito` y `retiro`, que tienen ancla determinista. Los motivos que pasan
# por LLM no tienen ancla y siguen midiendo la sesion entera, a la espera de la definicion
# del negocio sobre cual interaccion representa la nota.
from datetime import datetime, timedelta, timezone  # noqa: E402

_T0 = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)


def _m(seg, from_me, body="", note=False, media="chat"):
    return {"created_at": _T0 + timedelta(seconds=seg), "from_me": from_me,
            "is_note": note, "body": body, "media_type": media,
            "sent_from": "OPERATOR" if from_me else None}


def _dos_interacciones_de_deposito():
    """Primera interaccion: comprobante que nadie contesta. Segunda, dos dias despues."""
    return [
        _m(0, False, "les mando el comprobante de la recarga", media="image"),
        _m(1, True, "*Mel:* ", note=True),
        _m(22, True, "Mel *resuelto* la conversación", note=True),
        _m(180000, False, "buenas me recarga", media="image"),
        _m(180030, True, "*Arturo:* Estamos verificando tu comprobante"),
        _m(180090, True, "*Arturo:* tu saldo ya está disponible"),
        _m(180095, True, "Arturo *resuelto* la conversación", note=True),
    ]


def test_los_tiempos_persistidos_describen_la_interaccion_juzgada(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_de_deposito())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    sess = _session_row("sess1")
    # El CRM dice: arranco en la primera y se resolvio en la ULTIMA (50 h de ventana).
    sess["created_at"] = _T0
    sess["first_sent_message_at"] = _T0 + timedelta(seconds=180030)
    sess["resolved_at"] = _T0 + timedelta(seconds=180095)
    score_session_and_store(conn, sess, llm=None, op_map={})
    params = _params_of_upsert(conn)
    # La interaccion juzgada es la PRIMERA (el ancla es el primer comprobante): duro 22 s
    # y nadie contesto. Nada de 50 horas ni de una primera respuesta a las 50 h.
    assert params["resolution_seconds"] == 22.0
    assert params["first_response_seconds"] is None


def test_el_desenlace_del_cliente_se_PERSISTE_en_todas_las_filas(monkeypatch):
    # El desenlace se agrega en `dimensions` DESPUES de armar el record, y ese bloque es
    # facil de dejar mal ubicado: si cae dentro de un docstring sigue siendo Python valido,
    # los tests siguen verdes y la feature no persiste NADA. Paso el 2026-08-12 al partir el
    # arbol en commits, y no habia ni un test que lo cubriera. Ahora si.
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_de_deposito())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    dims = _params_of_upsert(conn)["dimensions"].obj
    assert "cliente_desenlace" in dims, "el desenlace no llego a la fila"
    assert "cliente_abandono" in dims, "el booleano viejo tampoco"


def test_se_persiste_DONDE_arranca_la_interaccion_juzgada(monkeypatch):
    # El front tiene que poder senalar CUAL de las interacciones se califico, y desde la fila
    # eso NO se puede deducir: cuando el ancla elige la PRIMERA, `conversation_created_at`
    # queda igual que si no hubiera ancla ninguna. Los dos casos son indistinguibles, y
    # adivinar significa senalar un tramo que nadie juzgo. Asi que se guarda.
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_de_deposito())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    dims = _params_of_upsert(conn)["dimensions"].obj
    assert dims["interaccion_juzgada_desde"] == _T0.isoformat()


def test_sin_ancla_NO_se_persiste_ninguna_interaccion(monkeypatch):
    # Sin ancla el LLM leyo la sesion COMPLETA: no hay UNA interaccion que senalar, y dejar
    # el campo vacio es lo unico honesto. El front marca todas, que es lo que paso.
    from src.scorer import ScoreResult
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_de_deposito())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: ScoreResult(
        rubric="promo", dimensions={}, rating_label="buena", rating_rationale="ok",
        stars=4, llm_model="fake", atencion=None, deposit_observed=None, motivo="promo"))
    conn = _CtxConn()
    score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    dims = _params_of_upsert(conn)["dimensions"].obj
    assert dims.get("interaccion_juzgada_desde") is None


def test_sin_ancla_determinista_los_tiempos_siguen_siendo_los_del_crm(monkeypatch):
    # Un motivo que pasa por LLM no tiene ancla: se degrada al comportamiento anterior.
    from src.scorer import ScoreResult
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_de_deposito())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: ScoreResult(
        rubric="promo", dimensions={}, rating_label="buena", rating_rationale="ok",
        stars=4, llm_model="fake", atencion=None, deposit_observed=None, motivo="promo"))
    conn = _CtxConn()
    sess = _session_row("sess1")
    sess["created_at"] = _T0
    sess["first_sent_message_at"] = _T0 + timedelta(seconds=180030)
    sess["resolved_at"] = _T0 + timedelta(seconds=180095)
    score_session_and_store(conn, sess, llm=None, op_map={})
    params = _params_of_upsert(conn)
    assert params["resolution_seconds"] == 180095.0


# --- LOS TIEMPOS SALEN DEL TRANSCRIPT, NUNCA DEL ENVASE DEL CRM ---------------------
# Cerrado el 2026-08-12 tras medir la corrida v6: el ventaneo tapaba el 100% del camino
# DETERMINISTA (91 de 91) y el 0% del fall-through al LLM (10 de 10 sin tocar), donde los
# tiempos volvian a los campos del CRM. Y esos campos son del ENVASE: `first_sent_message_at`
# puede ser de OTRA interaccion que `created_at` -- es el 51,5 h de `f9b31f4f`.
# NO se acota la ventana del fall-through a proposito: ahi el LLM leyo la sesion COMPLETA, y
# elegir una interaccion seria decidir por el negocio cual representa la nota (sigue abierto).
# Lo que se corrige es de DONDE salen los numeros: del transcript que se juzgo, siempre.

def _dos_interacciones_sin_ancla():
    """Sesion de 2 interacciones sin transaccion: cae al LLM, no hay ancla determinista."""
    return [
        _m(0, False, "hola, una consulta sobre la promo"),
        _m(30, True, "*Mel:* te cuento"),
        _m(60, True, "Mel *resuelto* la conversación", note=True),
        _m(90000, False, "otra cosa"),
        _m(90030, True, "*Mel:* dale"),
        _m(90060, True, "Mel *resuelto* la conversación", note=True),
    ]


def test_sin_ancla_los_tiempos_salen_del_transcript_no_del_crm(monkeypatch):
    from src.scorer import ScoreResult
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _dos_interacciones_sin_ancla())
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: ScoreResult(
        rubric="promo", dimensions={}, rating_label="buena", rating_rationale="ok",
        stars=4, llm_model="qwen3:14b", atencion=None, deposit_observed=None, motivo="promo"))
    conn = _CtxConn()
    sess = _session_row("sess1")
    # El CRM miente en las dos puntas: created_at ANTES del primer mensaje, y
    # first_sent_message_at de la SEGUNDA interaccion (25 h despues).
    sess["created_at"] = _T0 - timedelta(hours=10)
    sess["first_sent_message_at"] = _T0 + timedelta(seconds=90030)
    sess["resolved_at"] = _T0 + timedelta(seconds=90060)
    score_session_and_store(conn, sess, llm=None, op_map={})
    params = _params_of_upsert(conn)
    # La sesion entera: del primer mensaje real (0) al ULTIMO cierre (90060).
    assert params["resolution_seconds"] == 90060.0
    # Y la primera respuesta es la REAL (30 s), no la del campo del CRM (90.030 s).
    assert params["first_response_seconds"] == 30.0


def test_el_cierre_de_una_sesion_completa_es_el_ULTIMO_no_el_primero():
    from src.interacciones import tiempos_de
    msgs = _dos_interacciones_sin_ancla()
    inicio, primera_op, cierre = tiempos_de(msgs)
    assert inicio == _T0
    assert primera_op == _T0 + timedelta(seconds=30)
    assert cierre == _T0 + timedelta(seconds=90060)
