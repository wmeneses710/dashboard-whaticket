"""Tests de la sesionizacion (D1). El grueso valida la funcion PURA assign_sessions
con datos en memoria (sin BD). Para refresh_account_sessions se usa un cursor falso
que despacha resultados por query (NO ejecuta SQL): valida ESTRUCTURA, agregados y
scoping por cuenta. El cursor falso no puede detectar errores de SQL (tipos, ON
CONFLICT, DISTINCT ON); eso se valida corriendo refresh_account_sessions contra la
copia real whaticket_copia (validacion manual, gotcha conocido del proyecto).
"""
from datetime import datetime, timedelta

import src.sessions as sess
from src.sessions import assign_sessions

BASE = datetime(2026, 1, 1, 8, 0, 0)


def _ep(conv, hours, body=None):
    """Episodio: conversation_id, created_at = BASE + hours, last_agent_body."""
    return {"conversation_id": conv, "created_at": BASE + timedelta(hours=hours),
            "last_agent_body": body}


# --- funcion PURA assign_sessions ---------------------------------------------

def test_episodio_unico():
    out = assign_sessions([_ep("a", 0)])
    assert out == [{"conversation_id": "a", "sess_no": 0, "session_id": "a"}]


def test_gap_menor_6h_misma_sesion():
    out = assign_sessions([_ep("a", 0), _ep("b", 3)])
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_encadenamiento_varios_menores_6h():
    out = assign_sessions([_ep("a", 0), _ep("b", 2), _ep("c", 4), _ep("d", 6)])
    assert [o["sess_no"] for o in out] == [0, 0, 0, 0]
    assert {o["session_id"] for o in out} == {"a"}


def test_corte_basico_por_gap_mayor_6h():
    out = assign_sessions([_ep("a", 0), _ep("b", 7)])  # gap 7h > 6h, sin cierre diferido
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_rescate_por_override_gap_6_48h_con_cierre_diferido():
    # gap 10h en (6h, 48h] y el episodio previo cierra con senal diferida -> NO corta.
    eps = [_ep("a", 0, body="dale, cuando puedas retomamos"), _ep("b", 10)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_no_rescate_si_gap_mayor_48h_aunque_haya_cierre():
    # gap 50h > 48h: corta aunque el previo tenga cierre diferido.
    eps = [_ep("a", 0, body="cuando puedas me escribes"), _ep("b", 50)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_no_rescate_si_gap_6_48h_pero_sin_cierre_diferido():
    # gap 10h en ventana, pero el previo no cierra con senal diferida -> corta.
    eps = [_ep("a", 0, body="listo, muchas gracias"), _ep("b", 10)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1]
    assert [o["session_id"] for o in out] == ["a", "b"]


def test_session_id_es_primer_conversation_id_de_la_sesion():
    # a(0) | corte b(7) | c a +1h de b (misma sesion que b).
    out = assign_sessions([_ep("a", 0), _ep("b", 7), _ep("c", 8)])
    assert [(o["conversation_id"], o["sess_no"], o["session_id"]) for o in out] == [
        ("a", 0, "a"),
        ("b", 1, "b"),
        ("c", 1, "b"),
    ]


def test_cierre_diferido_se_evalua_sobre_el_episodio_previo():
    # El body diferido esta en 'b' (previo a 'c'), no en 'a'. Corte a->b (a sin cierre),
    # rescate b->c (b con cierre, gap en ventana).
    eps = [_ep("a", 0), _ep("b", 7, body="cualquier duda me avisas"), _ep("c", 15)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 1, 1]
    assert [o["session_id"] for o in out] == ["a", "b", "b"]


def test_body_none_no_rompe():
    out = assign_sessions([_ep("a", 0, body=None), _ep("b", 7, body=None)])
    assert [o["sess_no"] for o in out] == [0, 1]


def test_lista_vacia():
    assert assign_sessions([]) == []


def test_gap_exacto_6h_no_corta():
    # Borde: gap == GAP (6h). La regla usa `>` estricto -> 6h EXACTAS no cortan.
    out = assign_sessions([_ep("a", 0), _ep("b", 6)])
    assert [o["sess_no"] for o in out] == [0, 0]
    assert [o["session_id"] for o in out] == ["a", "a"]


def test_gap_exacto_48h_con_cierre_rescata():
    # Borde: gap == GAP_EXT (48h) con cierre diferido -> rescata (usa `<=`).
    eps = [_ep("a", 0, body="cuando puedas retomamos"), _ep("b", 48)]
    out = assign_sessions(eps)
    assert [o["sess_no"] for o in out] == [0, 0]


def test_encadenamiento_dos_cortes():
    # Tres sesiones: a(0) | corte b(+7h) | corte c(+14h), sin cierres diferidos.
    out = assign_sessions([_ep("a", 0), _ep("b", 7), _ep("c", 14)])
    assert [o["sess_no"] for o in out] == [0, 1, 2]
    assert [o["session_id"] for o in out] == ["a", "b", "c"]


def test_entrada_desordenada_se_ordena_internamente():
    # La funcion no depende del orden del caller: pasa desordenado, ordena por created_at.
    out = assign_sessions([_ep("b", 7), _ep("a", 0)])
    assert [(o["conversation_id"], o["sess_no"]) for o in out] == [("a", 0), ("b", 1)]


def test_refresh_barre_huerfanas():
    # El refresh debe emitir el DELETE de huerfanas scopeado por cuenta.
    cur = _FakeCursor(
        msg_rows=[("c1", "listo gracias")],
        conv_rows=[("t1", "c1", BASE)],
    )
    sess.refresh_account_sessions(cur, "datos")
    deletes = [(q, p) for q, p in cur.executed
               if "DELETE FROM conversation_sessions" in q]
    assert deletes and deletes[0][1] == {"account": "datos"}


# --- ensure_sessions_table / refresh_account_sessions -------------------------

class _FakeCursor:
    """Cursor falso que despacha fetchall por el ultimo query ejecutado."""

    def __init__(self, msg_rows=(), conv_rows=()):
        self.executed = []
        self.executed_many = []
        self._msg_rows = msg_rows
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
        if "FROM messages" in self._last:
            return self._msg_rows
        if "FROM conversations" in self._last:
            return self._conv_rows
        return []


def test_ensure_sessions_table_crea_tablas_e_indices():
    cur = _FakeCursor()
    sess.ensure_sessions_table(cur)
    qs = [q for q, _ in cur.executed]
    assert any("CREATE TABLE IF NOT EXISTS conversation_sessions" in q for q in qs)
    assert any("CREATE TABLE IF NOT EXISTS conversation_session_map" in q for q in qs)
    assert sum("CREATE INDEX IF NOT EXISTS" in q for q in qs) >= 3


def test_refresh_scopeado_por_cuenta_y_asegura_tabla():
    cur = _FakeCursor(msg_rows=[], conv_rows=[])
    sess.refresh_account_sessions(cur, "datos")
    assert any("CREATE TABLE IF NOT EXISTS conversation_sessions" in q for q, _ in cur.executed)
    # ambos SELECT llevan el account como param
    selects = [(q, p) for q, p in cur.executed if "SELECT" in q and "FROM" in q]
    assert selects and all(p == {"account": "datos"} for _, p in selects)


def test_refresh_materializa_sesiones_y_mapeo_con_override():
    # ticket t1: c1(0) + c2(+10h). c1 cierra diferido -> rescate -> 1 sola sesion.
    cur = _FakeCursor(
        msg_rows=[("c1", "cuando puedas retomamos")],
        conv_rows=[("t1", "c1", BASE), ("t1", "c2", BASE + timedelta(hours=10))],
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
    assert start_at == BASE and end_at == BASE + timedelta(hours=10)
    assert episode_count == 2

    q_map, map_rows = map_upserts[0]
    assert "ON CONFLICT (conversation_id) DO UPDATE" in q_map
    assert set(map_rows) == {("c1", "sistemas", "c1"), ("c2", "sistemas", "c1")}


def test_refresh_corta_en_dos_sesiones_sin_cierre():
    # sin cierre diferido y gap 10h -> dos sesiones.
    cur = _FakeCursor(
        msg_rows=[("c1", "listo gracias")],
        conv_rows=[("t1", "c1", BASE), ("t1", "c2", BASE + timedelta(hours=10))],
    )
    n = sess.refresh_account_sessions(cur, "datos")
    assert n == 2
    _, map_rows = [(q, seq) for q, seq in cur.executed_many
                   if "INSERT INTO conversation_session_map" in q][0]
    assert set(map_rows) == {("c1", "datos", "c1"), ("c2", "datos", "c2")}


def test_refresh_sin_conversaciones_devuelve_cero():
    cur = _FakeCursor(msg_rows=[], conv_rows=[])
    assert sess.refresh_account_sessions(cur, "datos") == 0
    assert cur.executed_many == []
