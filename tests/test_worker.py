"""Tests del worker: lo verificable sin DB/LLM es la seleccion de pendientes."""
from src.worker import fetch_pending


class _FakeCursor:
    def __init__(self, rows=(), description=()):
        self._rows = rows
        self.description = [type("C", (), {"name": n})() for n in description]
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows


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
