"""Tests de la sesionizacion (D1). El grueso valida la funcion PURA assign_sessions
con datos en memoria (sin BD). Para refresh_account_sessions se usa un cursor falso
que despacha resultados por query (NO ejecuta SQL): valida ESTRUCTURA, agregados y
scoping por cuenta. El cursor falso no puede detectar errores de SQL (tipos, ON
CONFLICT, DISTINCT ON, row_number); eso se valida corriendo refresh_account_sessions
contra la copia real whaticket_copia (validacion manual, gotcha conocido del proyecto).
"""
from datetime import datetime, timedelta

import src.sessions as sess
from src.sessions import assign_sessions

BASE = datetime(2026, 1, 1, 8, 0, 0)


def _ep(conv, hours, body=None, agent=None):
    """Episodio: conversation_id, created_at = BASE + hours, last_agent_body, agent_id."""
    return {"conversation_id": conv, "created_at": BASE + timedelta(hours=hours),
            "last_agent_body": body, "agent_id": agent}


# --- funcion PURA assign_sessions ---------------------------------------------

def test_episodio_unico():
    out = assign_sessions([_ep("a", 0)])
    assert out == [{"conversation_id": "a", "sess_no": 0, "session_id": "a"}]


def test_gap_menor_5h_mismo_agente_sin_cierre_misma_sesion():
    out = assign_sessions([_ep("a", 0, agent="op1"), _ep("b", 3, agent="op1")])
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_encadenamiento_varios_menores_5h_mismo_agente():
    eps = [_ep("a", 0, agent="op1"), _ep("b", 2, agent="op1"),
           _ep("c", 4, agent="op1"), _ep("d", 6, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0, 0, 0]
    assert {o["session_id"] for o in out} == {"a"}


def test_corte_por_gap_mayor_5h():
    out = assign_sessions([_ep("a", 0), _ep("b", 6)])  # gap 6h > 5h -> corta
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_gap_exacto_5h_no_corta():
    # Borde: gap == GAP (5h). La regla usa `>` estricto -> 5h EXACTAS no cortan.
    out = assign_sessions([_ep("a", 0), _ep("b", 5)])
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_corte_si_previo_cierra_confirmacion_aunque_gap_menor():
    # El previo cierra (confirmacion de carga) -> corta aunque gap < 5h y mismo agente.
    eps = [_ep("a", 0, body="listo, ya te lo dejé cargado", agent="op1"),
           _ep("b", 2, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_corte_si_previo_cierra_despedida():
    eps = [_ep("a", 0, body="listo, éxitos!", agent="op1"), _ep("b", 1, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]


def test_corte_si_previo_cierra_diferido():
    eps = [_ep("a", 0, body="cuando quieras me avisás", agent="op1"),
           _ep("b", 2, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_corte_por_cambio_de_agente_aunque_gap_menor_y_sin_cierre():
    # Agente cambia (op1 -> op2), gap < 5h, sin cierre -> corta igual.
    eps = [_ep("a", 0, agent="op1"), _ep("b", 2, agent="op2")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_no_corta_si_agente_nulo_en_algun_lado():
    # agent_changed exige ambos no nulos: op1 -> None no corta (mergea).
    eps = [_ep("a", 0, agent="op1"), _ep("b", 2, agent=None)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0]


def test_corte_por_span_cap():
    # Cadena con gaps <5h que en total supera SPAN_CAP (12h) -> corta al pasar el span.
    # 0,4,8 mergean (span 8<=12); en 13 el span (13h) supera 12h -> corta.
    eps = [_ep("a", 0, agent="op1"), _ep("b", 4, agent="op1"),
           _ep("c", 8, agent="op1"), _ep("d", 13, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0, 0, 1]
    assert [o["session_id"] for o in out] == ["a", "a", "a", "d"]


def test_span_cap_reinicia_desde_inicio_de_sesion():
    # El span se mide desde session_start, no desde el episodio previo. Tras cortar en
    # 'd' (span 13h), la nueva sesion arranca su propio span en 13h.
    eps = [_ep("a", 0, agent="op1"), _ep("b", 4, agent="op1"),
           _ep("c", 8, agent="op1"), _ep("d", 13, agent="op1"),
           _ep("e", 16, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0, 0, 1, 1]
    assert [o["session_id"] for o in out] == ["a", "a", "a", "d", "d"]


def test_episodio_solo_cliente_mergea_con_siguiente():
    # Episodio sin agente y sin cierre (solo-cliente) -> NO corta, mergea. Mata el
    # skip fabricado: el episodio hermano con agente cae en la misma sesion.
    eps = [_ep("a", 0, body=None, agent=None), _ep("b", 2, body=None, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_session_id_es_primer_conversation_id_de_la_sesion():
    # a(0) | corte b(6) | c a +1h de b (misma sesion que b).
    out = assign_sessions([_ep("a", 0), _ep("b", 6), _ep("c", 7)])
    assert [(o["conversation_id"], o["sess_no"], o["session_id"]) for o in out] == [
        ("a", 0, "a"),
        ("b", 1, "b"),
        ("c", 1, "b"),
    ]


def test_cierre_se_evalua_sobre_el_episodio_previo():
    # El cierre esta en 'b' (previo a 'c'): corte a->b por gap, corte b->c por cierre.
    eps = [_ep("a", 0, agent="op1"), _ep("b", 6, body="cualquier duda me avisás", agent="op1"),
           _ep("c", 8, agent="op1")]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1, 2]
    assert [o["session_id"] for o in out] == ["a", "b", "c"]


def test_body_none_no_rompe():
    out = assign_sessions([_ep("a", 0, body=None), _ep("b", 6, body=None)])
    assert [o["sess_no"] for o in out] == [0, 1]


def test_lista_vacia():
    assert assign_sessions([]) == []


def test_encadenamiento_dos_cortes():
    # Tres sesiones: a(0) | corte b(+6h) | corte c(+12h), por gap.
    out = assign_sessions([_ep("a", 0), _ep("b", 6), _ep("c", 12)])
    assert [o["sess_no"] for o in out] == [0, 1, 2]
    assert [o["session_id"] for o in out] == ["a", "b", "c"]


def test_entrada_desordenada_se_ordena_internamente():
    # La funcion no depende del orden del caller: pasa desordenado, ordena por created_at.
    out = assign_sessions([_ep("b", 6), _ep("a", 0)])
    assert [(o["conversation_id"], o["sess_no"]) for o in out] == [("a", 0), ("b", 1)]


# --- ensure_sessions_table / refresh_account_sessions -------------------------

class _FakeCursor:
    """Cursor falso que despacha fetchall por el ultimo query ejecutado."""

    def __init__(self, msg_rows=(), primary_rows=(), conv_rows=()):
        self.executed = []
        self.executed_many = []
        self._msg_rows = msg_rows
        self._primary_rows = primary_rows
        self._conv_rows = conv_rows
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))
        self._last = query

    def executemany(self, query, seq):
        self.executed_many.append((query, list(seq)))

    def fetchall(self):
        # _PRIMARY_AGENT_SQL tambien lee FROM messages, se distingue por row_number.
        if "row_number()" in self._last:
            return self._primary_rows
        if "FROM messages" in self._last:
            return self._msg_rows
        if "FROM conversations" in self._last:
            return self._conv_rows
        return []


def test_refresh_barre_huerfanas():
    # El refresh debe emitir el DELETE de huerfanas scopeado por cuenta.
    cur = _FakeCursor(
        msg_rows=[("c1", "listo gracias")],
        primary_rows=[("c1", "op1")],
        conv_rows=[("t1", "c1", BASE)],
    )
    sess.refresh_account_sessions(cur, "datos")
    deletes = [(q, p) for q, p in cur.executed
               if "DELETE FROM conversation_sessions" in q]
    assert deletes and deletes[0][1] == {"account": "datos"}


def test_ensure_sessions_table_crea_tablas_e_indices():
    cur = _FakeCursor()
    sess.ensure_sessions_table(cur)
    qs = [q for q, _ in cur.executed]
    assert any("CREATE TABLE IF NOT EXISTS conversation_sessions" in q for q in qs)
    assert any("CREATE TABLE IF NOT EXISTS conversation_session_map" in q for q in qs)
    assert sum("CREATE INDEX IF NOT EXISTS" in q for q in qs) >= 3


def test_refresh_scopeado_por_cuenta_y_asegura_tabla():
    cur = _FakeCursor(msg_rows=[], primary_rows=[], conv_rows=[])
    sess.refresh_account_sessions(cur, "datos")
    assert any("CREATE TABLE IF NOT EXISTS conversation_sessions" in q for q, _ in cur.executed)
    # los tres SELECT llevan el account como param
    selects = [(q, p) for q, p in cur.executed if "SELECT" in q and "FROM" in q]
    assert selects and all(p == {"account": "datos"} for _, p in selects)


def test_refresh_dispara_query_de_agente_dominante():
    # El refresh debe consultar el agente dominante (row_number) scopeado por cuenta.
    cur = _FakeCursor(msg_rows=[], primary_rows=[], conv_rows=[])
    sess.refresh_account_sessions(cur, "datos")
    primary = [(q, p) for q, p in cur.executed if "row_number()" in q]
    assert primary and primary[0][1] == {"account": "datos"}


def test_refresh_materializa_una_sesion_sin_cierre_mismo_agente():
    # ticket t1: c1(0) + c2(+3h). Sin cierre, mismo agente, gap<5h -> 1 sola sesion.
    cur = _FakeCursor(
        msg_rows=[("c1", "hola, en qué te ayudo")],
        primary_rows=[("c1", "op1"), ("c2", "op1")],
        conv_rows=[("t1", "c1", BASE), ("t1", "c2", BASE + timedelta(hours=3))],
    )
    n = sess.refresh_account_sessions(cur, "sistemas")
    assert n == 1  # una sesion materializada

    sess_upserts = [(q, seq) for q, seq in cur.executed_many
                    if "INSERT INTO conversation_sessions" in q]
    map_upserts = [(q, seq) for q, seq in cur.executed_many
                   if "INSERT INTO conversation_session_map" in q]
    assert len(sess_upserts) == 1 and len(map_upserts) == 1

    q_sess, sess_rows = sess_upserts[0]
    assert "ON CONFLICT (account, session_id) DO UPDATE" in q_sess
    assert len(sess_rows) == 1
    account, ticket_id, session_id, sess_no, start_at, end_at, episode_count = sess_rows[0]
    assert account == "sistemas" and ticket_id == "t1"
    assert session_id == "c1" and sess_no == 0
    assert start_at == BASE and end_at == BASE + timedelta(hours=3)
    assert episode_count == 2

    q_map, map_rows = map_upserts[0]
    assert "ON CONFLICT (conversation_id) DO UPDATE" in q_map
    assert set(map_rows) == {("c1", "sistemas", "c1"), ("c2", "sistemas", "c1")}


def test_refresh_corta_en_dos_sesiones_por_cierre_previo():
    # c1 cierra (confirmacion de carga) -> c2 arranca sesion nueva aunque gap<5h.
    cur = _FakeCursor(
        msg_rows=[("c1", "listo, ya te lo dejé cargado")],
        primary_rows=[("c1", "op1"), ("c2", "op1")],
        conv_rows=[("t1", "c1", BASE), ("t1", "c2", BASE + timedelta(hours=2))],
    )
    n = sess.refresh_account_sessions(cur, "datos")
    assert n == 2
    _, map_rows = [(q, seq) for q, seq in cur.executed_many
                   if "INSERT INTO conversation_session_map" in q][0]
    assert set(map_rows) == {("c1", "datos", "c1"), ("c2", "datos", "c2")}


def test_refresh_corta_por_cambio_de_agente():
    # Sin cierre, gap<5h, pero cambia el agente (op1 -> op2) -> dos sesiones.
    cur = _FakeCursor(
        msg_rows=[("c1", "hola")],
        primary_rows=[("c1", "op1"), ("c2", "op2")],
        conv_rows=[("t1", "c1", BASE), ("t1", "c2", BASE + timedelta(hours=2))],
    )
    n = sess.refresh_account_sessions(cur, "datos")
    assert n == 2
    _, map_rows = [(q, seq) for q, seq in cur.executed_many
                   if "INSERT INTO conversation_session_map" in q][0]
    assert set(map_rows) == {("c1", "datos", "c1"), ("c2", "datos", "c2")}


def test_refresh_sin_conversaciones_devuelve_cero():
    cur = _FakeCursor(msg_rows=[], primary_rows=[], conv_rows=[])
    assert sess.refresh_account_sessions(cur, "datos") == 0
    assert cur.executed_many == []
