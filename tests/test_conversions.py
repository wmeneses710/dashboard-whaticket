"""Tests del pase de conversión. Ojo: _FakeCursor NO ejecuta SQL (solo lo guarda),
así que esto valida ESTRUCTURA y params; la corrección del SQL se verifica contra
la BD real (los ids son uuid, etc.)."""
import src.conversions as conv


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 7

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return []


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
