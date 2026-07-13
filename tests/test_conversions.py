"""Tests del pase de conversión. Ojo: _FakeCursor NO ejecuta SQL (solo lo guarda),
así que esto valida ESTRUCTURA y params; la corrección del SQL se verifica contra
la BD real (los ids son uuid, etc.)."""
import src.conversions as conv


class _FakeCursor:
    def __init__(self, fetch=()):
        self.executed = []
        self.rowcount = 7
        self._fetch = fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._fetch


class _FakeConn:
    def __init__(self, pending):
        self._pending = pending
        self.commits = 0
        self.cursors = []

    def cursor(self):
        c = _FakeCursor(self._pending)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_ensure_table_crea_tabla_e_indices():
    cur = _FakeCursor()
    conv.ensure_table(cur)
    qs = [q for q, _ in cur.executed]
    assert any("CREATE TABLE IF NOT EXISTS player_conversions" in q for q in qs)
    assert sum("CREATE INDEX IF NOT EXISTS" in q for q in qs) == 2


def test_refresh_sin_colas_jugador_no_computa(monkeypatch):
    monkeypatch.setattr(conv, "_jugador_queue_ids", lambda cur, account: [])
    cur = _FakeCursor()
    n = conv.refresh_account_conversions(cur, "datos")
    assert n == 0
    assert not any("INSERT INTO player_conversions" in q for q, _ in cur.executed)


def test_refresh_upsert_determinista_scopeado_por_cuenta(monkeypatch):
    monkeypatch.setattr(conv, "_jugador_queue_ids", lambda cur, account: ["q1", "q2"])
    cur = _FakeCursor()
    conv.refresh_account_conversions(cur, "sistemas")
    ins = [(q, p) for q, p in cur.executed if "INSERT INTO player_conversions" in q]
    assert len(ins) == 1
    query, params = ins[0]
    # potencial = is_new_contact; entrada = 1ª conversación; upsert de deposited
    assert "is_new_contact" in query
    assert "DISTINCT ON (jc.contact_id)" in query
    assert "ON CONFLICT (account, contact_id) DO UPDATE" in query
    assert "t.contact_id::text" in query                 # evita mismatch de tipos
    # señal de depósito determinista (comprobante+recarga), sin LLM
    assert "%(re)s" in query and params["re"] == conv.RECHARGE_PATTERN
    assert params["account"] == "sistemas" and params["qids"] == ["q1", "q2"]


def test_classify_passivity_batch_pendientes_y_upsert(monkeypatch):
    import src.context, src.passivity
    monkeypatch.setattr(src.context, "fetch_messages", lambda cur, cid: [{"from_me": True, "body": "x"}])
    monkeypatch.setattr(src.passivity, "classify_passivity", lambda llm, msgs: "pasivo")
    conn = _FakeConn(pending=[("c1", "conv1"), ("c2", "conv2")])
    out = conv.classify_passivity_batch(conn, llm=None, account="datos", limit=20)
    assert out == {"seen": 2, "classified": 2}
    assert conn.commits == 2
    # el SELECT de pendientes filtra attention NULL y operador humano
    sel = conn.cursors[0].executed[0][0]
    assert "attention IS NULL" in sel and "user_id IS NOT NULL" in sel
    # se actualiza attention
    assert any("SET attention" in q for c in conn.cursors for q, _ in c.executed)


def test_classify_passivity_batch_skip_si_llm_none(monkeypatch):
    import src.context, src.passivity
    monkeypatch.setattr(src.context, "fetch_messages", lambda cur, cid: [])
    monkeypatch.setattr(src.passivity, "classify_passivity", lambda llm, msgs: None)  # inválido
    conn = _FakeConn(pending=[("c1", "conv1")])
    out = conv.classify_passivity_batch(conn, llm=None, account="datos")
    assert out == {"seen": 1, "classified": 0} and conn.commits == 0
