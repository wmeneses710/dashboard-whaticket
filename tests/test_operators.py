"""Tests de la extraccion del nombre del operador desde el cuerpo del mensaje.

La tabla users viene vacia, pero el operador firma cada mensaje con el prefijo
'*Nombre:*'. Reconstruimos user_id -> nombre desde ahi.
"""
from src.operators import operator_name


def test_extrae_nombre_del_prefijo():
    msgs = [
        {"from_me": True, "is_note": False, "user_id": "op-A", "body": "*Annel Flores:*\nBuenos dias"},
        {"from_me": True, "is_note": False, "user_id": "op-A", "body": "*Annel Flores:*\nlisto"},
    ]
    assert operator_name(msgs, "op-A") == "Annel Flores"


def test_toma_el_nombre_mas_frecuente():
    msgs = [
        {"from_me": True, "is_note": False, "user_id": "op-A", "body": "*Ana:*\nhola"},
        {"from_me": True, "is_note": False, "user_id": "op-A", "body": "*Ana:*\nsi"},
        {"from_me": True, "is_note": False, "user_id": "op-A", "body": "*Anna:*\ntipeo"},
    ]
    assert operator_name(msgs, "op-A") == "Ana"


def test_none_si_no_hay_prefijo():
    msgs = [{"from_me": True, "is_note": False, "user_id": "op-A", "body": "hola sin firma"}]
    assert operator_name(msgs, "op-A") is None


def test_ignora_mensajes_de_otros_operadores():
    msgs = [{"from_me": True, "is_note": False, "user_id": "op-B", "body": "*Otro:*\nhi"}]
    assert operator_name(msgs, "op-A") is None


def test_none_si_operador_desconocido():
    assert operator_name([], None) is None


class _FakeCursor:
    """Cursor minimo: guarda las filas y las devuelve en fetchall."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        self._query = query
        self._params = params

    def fetchall(self):
        return self._rows


def test_build_operator_map_toma_el_nombre_mas_frecuente_por_operador():
    from src.operators import build_operator_map
    rows = [
        ("op-A", "Ana", 5),
        ("op-A", "Anna", 1),   # typo menos frecuente -> descartado
        ("op-B", "Beto", 3),
    ]
    m = build_operator_map(_FakeCursor(rows))
    assert m == {"op-A": "Ana", "op-B": "Beto"}


def test_build_operator_map_scopea_por_cuenta():
    from src.operators import build_operator_map
    cur = _FakeCursor([])
    build_operator_map(cur, account="datos")
    assert "datos" in (cur._params or ())
