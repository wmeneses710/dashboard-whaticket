"""Tests del worker: lo verificable sin DB/LLM es la seleccion de pendientes."""
import src.worker as worker
from src.worker import (
    fetch_pending,
    fetch_pending_sessions,
    score_and_store,
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


def test_nadie_le_respondio_ahora_lleva_UNA_estrella(monkeypatch):
    """CAMBIO DEL 2026-08-21. Esto era `skipped/no_agent_reply` -- 1.167 sesiones donde el
    cliente escribio y nadie contesto, invisibles en todos los cuadros. Ahora llevan 1
    estrella determinista (src/sin_respuesta.py).

    La intencion del test viejo se conserva ENTERA: sigue sin correr el LLM. Pagar una
    inferencia para leer una conversacion donde el negocio no escribio nada es gasto puro.
    Lo que cambia es el resultado: nota en vez de skip.

    La fixture ahora trae `created_at` porque la consulta real SIEMPRE lo trae (el contrato
    lo fija tests/test_context.py); sin el, el fixture representaba algo que produccion no
    produce -- y solo pasaba porque el skip cortaba antes de que nada lo necesitara.
    """
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "me ayudas con una recarga?", "sent_from": None,
         "user_id": None, "media_type": None},
    ])

    def boom(**kw):
        raise AssertionError("no debe correr el LLM cuando nadie respondio")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    eval_status, skip_reason, score = score_session_and_store(
        conn, _session_row(), llm=None, op_map={})
    assert (eval_status, skip_reason) == ("evaluated", None)
    assert score is not None and score.stars == 1
    assert score.dimensions.get("sin_respuesta_del_negocio") is True
    params = _params_of_upsert(conn)
    assert params["session_id"] == "sess1" and params["stars"] == 1


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
    # Una sola vuelta del loop: alcanza para ver que intento el lock y no scoreo.
    vueltas = iter([False, True])
    worker.run_worker_loop(cfg, should_stop=lambda: next(vueltas, True))

    assert called["batch"] == 0        # no scoreó: el lock lo tiene otro
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


def test_un_traspaso_LIMPIO_se_saltea_y_no_llama_al_LLM(monkeypatch):
    """CAMBIO DEL 2026-08-24. El 2026-08-20 `redireccion` habia dejado de ser skip para poder
    contarla; ahora vuelve a serlo cuando el destino esta VIVO, por decision del negocio:
    "si es redireccion no deberia ni calificarse, porque es algo que no le compete, y la
    mayoria ni explica". Medido sobre 2.500 sesiones: 13 traspasos puros y **12 daban 4
    estrellas** -- una nota que califica igual al 92% no mide nada.
    Y no vuelve el problema que lo saco de skip: se sigue contando en la tarjeta de sin
    evaluar, que desglosa por causa y ya tiene la etiqueta `redireccion`.

    LA INTENCION DEL TEST VIEJO SE CONSERVA TAL CUAL: sigue sin pasar por el modelo. El
    traspaso puro lo decide una funcion pura y a donde apunta lo dice `connections`: dos
    hechos que el modelo no puede verificar, asi que pagar una inferencia seria gasto puro.
    """
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _redireccion_session_messages())

    def boom(**kw):
        raise AssertionError("no debe correr el LLM en una redireccion")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    eval_status, skip_reason, score = score_session_and_store(
        conn, _session_row(), llm=None, op_map={}, lineas={"991194133": "CONNECTED"})
    assert (eval_status, skip_reason) == ("skipped", "redireccion")
    assert score is None, "un traspaso a una línea viva no lleva nota"


def test_un_traspaso_SIN_destino_vivo_SI_se_califica(monkeypatch):
    """La excepcion sale del mismo argumento del negocio: "no le compete" vale para mandarlo
    a una linea viva. Elegir mandarlo a una CAIDA si le compete, y el cliente queda sin a
    donde escribir."""
    monkeypatch.setattr(worker, "fetch_session_messages",
                        lambda cur, sid: _redireccion_session_messages())
    monkeypatch.setattr(worker, "score_by_motivo",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("sin LLM")))
    conn = _CtxConn()
    eval_status, skip_reason, score = score_session_and_store(
        conn, _session_row(), llm=None, op_map={}, lineas={"991194133": "DISCONNECTED"})
    assert (eval_status, skip_reason) == ("evaluated", None)
    assert score is not None and score.motivo == "redireccion" and score.stars == 2


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
    # La interaccion juzgada es la ULTIMA (el ancla paso al ultimo comprobante el 2026-08-12,
    # ver src/deposito): arranca a los 180.000 s, el operador contesta 30 s despues y cierra a
    # los 95 s. Nada de las 50 horas del ENVASE, que es lo que este test protege.
    assert params["resolution_seconds"] == 95.0
    assert params["first_response_seconds"] == 30.0


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
    # La ULTIMA interaccion, no la primera: arranca a los 180.000 s del inicio de la sesion.
    assert dims["interaccion_juzgada_desde"] == (_T0 + timedelta(seconds=180000)).isoformat()


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


def test_el_nombre_de_las_NOTAS_rescata_al_operador_sin_user_id(monkeypatch):
    # 881 sesiones tienen mensajes humanos del negocio y NI user_id NI firma. El nombre estaba
    # en la nota de cierre del CRM, que ya leiamos para cortar interacciones. Sin esto salen
    # como "Operador sin identificar" y su trabajo queda sin dueño.
    msgs = [
        {"created_at": _T0, "from_me": False, "is_note": False, "body": "hola",
         "media_type": "chat", "sent_from": None},
        {"created_at": _T0 + timedelta(seconds=30), "from_me": True, "is_note": False,
         "body": "te ayudo con eso", "media_type": "chat", "sent_from": "OPERATOR"},
        {"created_at": _T0 + timedelta(seconds=60), "from_me": True, "is_note": True,
         "body": "Anggie Belén *resuelto* la conversación", "media_type": None},
    ]
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: msgs)
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    score_session_and_store(conn, _session_row("sess1"), llm=None, op_map={})
    params = _params_of_upsert(conn)
    assert params["user_name"] == "Anggie Belén", params["user_name"]
    assert params["user_id"] is None, "no se inventa un user_id: solo el nombre"


def test_la_ASIGNACION_del_crm_nombra_al_operador_cuando_no_hay_nada_mas(monkeypatch):
    # SEXTA y ultima puerta. `conversations.user_id` es una FK real a `users`, pero apunta a
    # quien TIENE la conversacion (se transfiere), no a quien la trabajo: medido contra la
    # verdad conocida acierta el 91%, contra el 99% de la nota de cierre. Por eso va ULTIMA.
    # Cierra el hueco exacto: de las 882 sesiones sin user_id ni firma, la nota rescata 860 y
    # la asignacion nombra a los 22 restantes.
    msgs = [
        {"created_at": _T0, "from_me": False, "is_note": False, "body": "hola",
         "media_type": "chat", "sent_from": None},
        {"created_at": _T0 + timedelta(seconds=30), "from_me": True, "is_note": False,
         "body": "te ayudo", "media_type": "chat", "sent_from": "OPERATOR"},
    ]
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: msgs)
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    sess = _session_row("sess1")
    sess["user_id"] = "232fcb19-5dd2-4c22-af04-9074c086488e"   # asignado en el CRM
    score_session_and_store(conn, sess, llm=None, op_map={sess["user_id"]: "Mario"})
    assert _params_of_upsert(conn)["user_name"] == "Mario"


def test_la_SEXTA_puerta_del_path_por_CONVERSACION_no_explota(monkeypatch):
    """`score_and_store` referenciaba `sess`, que no existe en su scope: NameError.

    HALLADO el 2026-08-14 leyendo el codigo. La sexta puerta de atribucion (la asignacion
    del CRM) se copio del path por SESION sin renombrar la variable: alli la fila se llama
    `sess`, aca `conv`. Solo se dispara cuando fallan las tres puertas previas -sin
    `user_id` en los mensajes, sin firma en el cuerpo y sin nota de cierre-, que es
    exactamente el caso que la puerta existe para cubrir.

    El loop del contenedor usa `score_session_and_store` y no pasa por aca; la ruta
    expuesta es `scripts/run_scoring.py`.
    """
    msgs = [
        {"created_at": _T0, "from_me": False, "is_note": False, "body": "hola",
         "media_type": "chat", "sent_from": None},
        # Operador SIN user_id y SIN firma, y no hay nota de cierre: las tres primeras
        # puertas devuelven None y la cuarta tiene que resolver.
        {"created_at": _T0 + timedelta(seconds=30), "from_me": True, "is_note": False,
         "body": "te ayudo", "media_type": "chat", "sent_from": "OPERATOR"},
    ]
    monkeypatch.setattr(worker, "fetch_messages", lambda cur, cid: msgs)
    monkeypatch.setattr(worker, "fetch_thread_context", lambda cur, tid, cid: "")
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    conv = _session_row("conv1")
    conv["user_id"] = "232fcb19-5dd2-4c22-af04-9074c086488e"   # asignado en el CRM
    score_and_store(conn, conv, llm=None, op_map={conv["user_id"]: "Mario"})
    assert _params_of_upsert(conn)["user_name"] == "Mario"


def test_la_NOTA_le_gana_a_la_asignacion(monkeypatch):
    # 99% contra 91%: si la nota nombra a alguien, esa manda. El caso real: asignada
    # automaticamente a Michelle y resuelta por Anya Alexandra -- trabajo Anya.
    msgs = [
        {"created_at": _T0, "from_me": False, "is_note": False, "body": "hola",
         "media_type": "chat", "sent_from": None},
        {"created_at": _T0 + timedelta(seconds=30), "from_me": True, "is_note": False,
         "body": "te ayudo", "media_type": "chat", "sent_from": "OPERATOR"},
        {"created_at": _T0 + timedelta(seconds=60), "from_me": True, "is_note": True,
         "body": "Anya Alexandra *resuelto* la conversación", "media_type": None},
    ]
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: msgs)
    monkeypatch.setattr(worker, "score_by_motivo", lambda **kw: _fake_score())
    conn = _CtxConn()
    sess = _session_row("sess1")
    sess["user_id"] = "abc"
    score_session_and_store(conn, sess, llm=None, op_map={"abc": "Michelle"})
    assert _params_of_upsert(conn)["user_name"] == "Anya Alexandra"


# --- UN GRUPO DE WHATSAPP NO SE CALIFICA -------------------------------------------
# El gate vive en src/router.decide_eligibility, pero sin el dato no protege nada: la
# marca `tickets.is_group` tiene que VIAJAR desde la BD hasta la llamada. Es la misma
# leccion que `ack` y `created_at` en tests/test_context.py -- si la columna no viene, la
# señal se degrada en silencio y ningun test de la capa pura lo ve.
# Medido en la copia del 2026-08-24: 4 de las 6 filas con 1 estrella eran grupos.

def test_el_sql_de_pendientes_trae_la_marca_de_grupo():
    cur = _FakeCursor([], description=[])
    fetch_pending_sessions(cur, "datos", 30)
    query, _ = cur.executed[0]
    assert "is_group" in query, "sin is_group en el SELECT el gate de grupos nunca dispara"
    assert "tickets" in query, "is_group vive en tickets: falta el JOIN"
    # LEFT JOIN a proposito: la mitad de las sesiones pendientes no tiene fila en
    # `tickets` (70.880 de 139.708 medidas), y un JOIN duro las borraria del padron.
    assert "LEFT JOIN tickets" in query


def test_una_sesion_de_grupo_se_saltea_sin_llamar_al_llm(monkeypatch):
    """El caso real: spam entrante de tipsters, el operador lo cerro sin contestar."""
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "*RETO ESCALERA VERDE, 13.500 pesos ganados EN SOLO 3 apuestas*",
         "sent_from": None, "user_id": None, "media_type": None},
    ])

    def boom(**kw):
        raise AssertionError("no debe correr el LLM en un grupo")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    row = _session_row()
    row["is_group"] = True
    eval_status, skip_reason, score = score_session_and_store(
        conn, row, llm=None, op_map={})
    assert (eval_status, skip_reason) == ("skipped", "grupo_de_whatsapp")
    assert score is None, "un grupo no lleva nota: no hay UN cliente al que atender"


def test_la_misma_sesion_sin_la_marca_sigue_llevando_su_estrella(monkeypatch):
    """El cambio NO tapa la falla real: solo deja de cobrarsela a quien atendio un grupo."""
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "me ayudas con una recarga?", "sent_from": None,
         "user_id": None, "media_type": None},
    ])
    conn = _CtxConn()
    row = _session_row()
    row["is_group"] = False
    eval_status, skip_reason, score = score_session_and_store(
        conn, row, llm=None, op_map={})
    assert (eval_status, skip_reason) == ("evaluated", None)
    assert score is not None and score.stars == 1


# --- EL WORKER TIENE QUE PODER VERSE EN LOS LOGS ---------------------------------------
# EL SINTOMA REAL (2026-08-24): el negocio dejo el scoring corriendo todo el fin de semana,
# volvio con ~500 filas y en los logs del contenedor **no habia una sola linea `[worker]`**:
# solo el access log de uvicorn. Sin eso no hay forma de saber si el worker esta trabajando,
# fallando en cada sesion, o muerto -- y las tres cosas se ven igual desde afuera, porque el
# worker corre como THREAD DAEMON dentro del mismo proceso que la API (src/app.py:110): la web
# sigue contestando 200 aunque el thread se haya detenido.
#
# LA CAUSA: `emit` usaba `print` pelado. El stdout de un contenedor NO es un TTY, asi que
# Python lo deja block-buffered y las lineas se quedan en un buffer de 4-8 KB sin salir nunca.
# uvicorn no sufre esto porque escribe por `logging`. El pre-flight `check_model()` -- el que
# avisa si el modelo configurado no existe en Ollama -- caia en el mismo pozo: el aviso mas
# importante del arranque era el mas invisible.

class _StreamQueRegistraFlush:
    def __init__(self):
        self.escrito, self.flushes = [], 0

    def write(self, s):
        self.escrito.append(s)
        return len(s)

    def flush(self):
        self.flushes += 1


def test_el_log_del_worker_hace_flush(monkeypatch):
    stream = _StreamQueRegistraFlush()
    monkeypatch.setattr("sys.stdout", stream)
    worker._emit_stdout("[worker] hola")
    assert "".join(stream.escrito).strip() == "[worker] hola"
    assert stream.flushes >= 1, (
        "sin flush la linea queda en el buffer del contenedor y el operador no ve nada")


def test_el_loop_usa_ese_log_por_defecto():
    """Si el default vuelve a ser `print` pelado, el worker se vuelve invisible otra vez."""
    import inspect

    firma = inspect.signature(worker.run_worker_loop)
    assert firma.parameters["log"].default is worker._emit_stdout


# --- EL LOCK TIENE QUE REINTENTARSE, NO RENDIRSE --------------------------------------
# LO QUE PASO DE VERDAD (logs de produccion del 2026-08-21):
#   19:26:04 [worker] lock de scoring adquirido (instancia única)
#   ... 4 ciclos sanos, err=0 en todos, preflight ok con gemma4:12b ...
#   21:58:45 Started server process [1]          <- el contenedor REINICIO
#   21:58:45 [worker] otra instancia ya tiene el lock de scoring; esta instancia NO scorea
# El advisory lock sigue atado a la SESION de Postgres de la instancia vieja, y si el proceso
# muere de golpe esa conexion puede tardar en que el servidor la reape (keepalives de TCP).
# La instancia nueva lo intento UNA vez, escribio esa linea y `return` -> el thread murio.
# Resultado: la web sirviendo 200 todo el fin de semana y CERO filas nuevas. Tres dias
# perdidos por un reintento que no existia.

def _cfg_de_prueba():
    import types
    return types.SimpleNamespace(
        database_url="postgresql://x", ollama_url="http://x", ollama_model="qwen",
        ollama_token="", recom_subagent_enabled=False,
        scoring_accounts=("sistemas",), scoring_batch_size=20, scoring_poll_seconds=1,
        ollama_num_ctx=16384, ollama_num_predict=768, llm_fast_attempts=2,
    )


def _fake_llm(monkeypatch):
    class _FakeLLM:
        def __init__(self, *a, **k):
            self.model = "qwen"
            self.calls = {"fast": 0, "fallback": 0, "empty": 0}

        def check_model(self):
            return (True, "ok")

    monkeypatch.setattr(worker, "OllamaClient", _FakeLLM)


def test_el_worker_REINTENTA_el_lock_en_vez_de_retirarse(monkeypatch):
    """Sin esto, un reinicio con el lock viejo todavía colgado cuesta todo hasta que alguien
    lo note a mano."""
    import types

    import psycopg

    intentos = {"n": 0}

    class _LockConn:
        autocommit = False

        def execute(self, *a, **k):
            intentos["n"] += 1
            return types.SimpleNamespace(fetchone=lambda: [False])

        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _LockConn())
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    _fake_llm(monkeypatch)

    vueltas = iter([False, False, False, True])
    worker.run_worker_loop(_cfg_de_prueba(), should_stop=lambda: next(vueltas, True))
    assert intentos["n"] >= 2, (
        f"solo intento el lock {intentos['n']} vez/veces: se rindio en vez de reintentar")


def test_cuando_el_lock_SE_LIBERA_el_worker_arranca_solo(monkeypatch):
    """Es el punto del reintento: que la instancia nueva se recupere sin que nadie reinicie
    nada cuando la sesion zombi de Postgres por fin se cae."""
    import types

    import psycopg

    respuestas = iter([False, True])

    class _LockConn:
        autocommit = False

        def execute(self, *a, **k):
            return types.SimpleNamespace(fetchone=lambda: [next(respuestas, True)])

        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _LockConn())
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    _fake_llm(monkeypatch)
    lineas = []
    # `_dormir` tambien consulta `should_stop` en cada tramo de 1s, asi que la cuenta no es
    # "una vuelta = una consulta": se le da margen y se corta cuando ya tomo el lock.
    def should_stop():
        return any("adquirido" in x for x in lineas)

    worker.run_worker_loop(_cfg_de_prueba(), should_stop=should_stop, log=lineas.append)
    texto = "\n".join(lineas)
    assert "lock de scoring adquirido" in texto, (
        f"nunca tomo el lock aunque se libero:\n{texto}")


# --- LA RAMA `agente` TAMBIEN TIENE QUE ACOTAR LA VENTANA ------------------------------
# EL AGUJERO: `_ANCLA_POR_MOTIVO` se consultaba SOLO dentro del `else` (el camino del
# jugador), asi que en el segmento `agente` `ventana_juzgada` quedaba en None SIEMPRE. Medido
# en la copia del 2026-08-24:
#     segment = jugador  -> 100% con ancla (deposito 27/27, registro 33/33, retiro 3/3)
#     segment = agente   ->   0% con ancla (deposito 0/90, retiro 0/23)
# Arrastra TRES cosas, porque `ventana_juzgada` gobierna las tres:
#   1. `interaccion_juzgada_desde` no se persiste -> el tablero no puede marcar CUAL de las
#      interacciones se califico. Caso real traido por el negocio: una sesion de 5
#      interacciones y 2 operadores donde la nota hablaba de la tercera y no habia forma de
#      saberlo.
#   2. `tiempos_de(ventana)` describe la SESION entera en vez de la interaccion juzgada.
#   3. EL OPERADOR. El bloque que reatribuye la nota al dueño de la ventana (`if
#      ventana_juzgada:`) no se ejecuta, asi que la nota va al operador dominante de toda la
#      sesion. Medido: **2 de 113 filas de agente estan mal atribuidas** -- las dos de 5
#      estrellas, o sea alguien cobrando el trabajo de otro (`62fdbf2b` la nota a Mel cuando
#      la ventana era de Joseph, `9a3ce7c1` a Arturo cuando era de Mel). Acertaba en 111 por
#      CASUALIDAD: el dominante de la sesion solia ser el mismo.

def _session_row_agente():
    fila = _session_row()
    fila["queue_name"] = "Agente 🍀"   # segment_for_queue -> "agente"
    return fila


def _dos_recargas():
    """Dos interacciones de recarga separadas por el cierre del CRM, con operadores
    DISTINTOS. Espeja la sesion real `d5c68b78` que trajo el negocio."""
    from datetime import timedelta

    def m(seg, from_me, body, *, note=False, uid=None, media=None):
        return {"created_at": _T0 + timedelta(seconds=seg), "from_me": from_me,
                "is_note": note, "body": body, "sent_from": "OP" if from_me else None,
                "user_id": uid, "media_type": media}

    return [
        m(0, False, None, media="image"), m(1, False, "Abono $5 a deuda"),
        m(60, True, "ing", uid="mel"),
        m(63, True, "Mel *resuelto* la conversación", note=True),
        # segunda recarga, veinte minutos despues y con OTRO operador
        m(1200, False, None, media="image"), m(1201, False, "Abono $5 a deuda"),
        m(1560, True, "ingreso", uid="joseph"),
        m(1563, True, "Joseph *resuelto* la conversación", note=True),
    ]


def test_una_sesion_de_agente_declara_QUE_interaccion_juzgo(monkeypatch):
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: _dos_recargas())

    def boom(**kw):
        raise AssertionError("el segmento agente no debe llamar al LLM")

    monkeypatch.setattr(worker, "score_by_motivo", boom)
    conn = _CtxConn()
    worker.score_session_and_store(conn, _session_row_agente(), llm=None, op_map={})
    params = _params_of_upsert(conn)
    # `upsert_score` envuelve el jsonb en `psycopg.types.json.Jsonb`; el dict vive en `.obj`.
    crudo = params["dimensions"]
    dims = getattr(crudo, "obj", crudo)
    assert "interaccion_juzgada_desde" in dims, (
        "la fila no dice cuál de las interacciones se calificó: en una sesión con varias y "
        "con operadores distintos, el que mira no puede verificar la nota")


def test_la_nota_de_agente_va_al_operador_de_la_VENTANA(monkeypatch):
    """Sin esto la nota va al dominante de toda la sesión, y en la copia eso ya puso 2 notas
    de 5 estrellas en la persona equivocada."""
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: _dos_recargas())
    monkeypatch.setattr(worker, "score_by_motivo",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("sin LLM")))
    conn = _CtxConn()
    worker.score_session_and_store(conn, _session_row_agente(), llm=None,
                                   op_map={"mel": "Mel", "joseph": "Joseph"})
    params = _params_of_upsert(conn)
    # El ancla de deposito es el ULTIMO comprobante (ancla-ultima, v12): la ventana juzgada es
    # la SEGUNDA recarga, que atendio Joseph.
    assert params["user_name"] == "Joseph", (
        f"la nota describe la interacción de Joseph y se la llevó {params['user_name']!r}")
