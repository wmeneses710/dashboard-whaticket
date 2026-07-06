"""Tests de la capa de queries: lo importante es que TODA lectura de scores
esta scopeada por cuenta (datos vs sistemas conviven en la misma BD)."""
from decimal import Decimal

from src.queries import conversation_detail, scored_rows


class _FakeCursor:
    def __init__(self, rows=(), description=(), one=None):
        self._rows = rows
        self._one = one
        self.description = [type("C", (), {"name": n})() for n in description]
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._one is not None:
            return self._one
        return self._rows[0] if self._rows else None


def test_scored_rows_siempre_filtra_por_cuenta():
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, params = cur.executed[0]
    assert "cs.account = %(account)s" in query
    assert params["account"] == "datos"


def test_scored_rows_devuelve_dicts_por_columna():
    cur = _FakeCursor(
        [("c1", "sistemas", "buena")],
        description=["conversation_id", "account", "rating_label"],
    )
    rows = scored_rows(cur, "sistemas")
    assert rows == [{"conversation_id": "c1", "account": "sistemas", "rating_label": "buena"}]


def test_scored_rows_coacciona_decimal_a_numero():
    # Postgres numeric -> Decimal en psycopg -> si sale como string en el JSON,
    # el front concatena en vez de sumar (bug del 7.19e+46). Se coacciona aca.
    cur = _FakeCursor(
        [("c1", Decimal("5"), Decimal("12.5"))],
        description=["conversation_id", "stars", "resolution_seconds"],
    )
    rows = scored_rows(cur, "datos")
    assert rows[0]["stars"] == 5.0 and isinstance(rows[0]["stars"], float)
    assert rows[0]["resolution_seconds"] == 12.5 and isinstance(rows[0]["resolution_seconds"], float)


def test_scored_rows_resuelve_operador_por_users():
    # Fuente canonica del nombre = tabla `users` (poblada por el monitor del ETL).
    # La firma '*Nombre:*' (cs.user_name) queda solo de fallback: COALESCE.
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    assert "JOIN users" in query
    assert "COALESCE(u.name, cs.user_name) AS user_name" in query


def test_scored_rows_incluye_contact_id_para_agrupar_por_cliente():
    # El front agrupa las tarjetas por contact_id (una persona = una tarjeta),
    # no por ticket. Debe venir como columna devuelta, no solo en el JOIN.
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    assert "AS contact_id" in query


def test_conversation_detail_coacciona_decimal_a_numero():
    cur = _FakeCursor(rows=[], description=["conversation_id", "stars"], one=("c1", Decimal("4")))
    d = conversation_detail(cur, "c1")
    assert d["stars"] == 4.0 and isinstance(d["stars"], float)


def test_conversation_detail_filtra_por_id_y_agrega_transcript():
    # fetchone -> fila de detalle; fetchall -> mensajes (vacio aqui)
    cur = _FakeCursor(rows=[], description=["conversation_id"], one=("c1",))
    d = conversation_detail(cur, "c1")
    query, params = cur.executed[0]
    assert "conversation_id = %(cid)s" in query
    assert params["cid"] == "c1"
    assert d["conversation_id"] == "c1"
    assert d["transcript"] == []
