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


def test_ensure_table_crea_tabla_e_indices():
    cur = _FakeCursor()
    conv.ensure_table(cur)
    qs = [q for q, _ in cur.executed]
    assert any("CREATE TABLE IF NOT EXISTS player_conversions" in q for q in qs)
    assert sum("CREATE INDEX IF NOT EXISTS" in q for q in qs) == 2


def test_ensure_table_altera_columnas_de_reengagement():
    # self-healing: tablas creadas por una versión previa reciben returned/return_session_id
    cur = _FakeCursor()
    conv.ensure_table(cur)
    qs = [q for q, _ in cur.executed]
    assert any(
        "ADD COLUMN IF NOT EXISTS returned boolean NOT NULL DEFAULT false" in q for q in qs
    )
    assert any("ADD COLUMN IF NOT EXISTS return_session_id uuid" in q for q in qs)


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
    # attention viene del SCORE (determinista, sin LLM) de la conversación de ENTRADA.
    # Ya NO se joinea `conversation_scores` directo: desde el cambio de grano tiene una
    # fila por INTERACCION y multiplicaba el upsert (ver el test de cardinalidad abajo).
    assert "score_de_entrada cs" in query
    assert "FROM conversation_scores" in query
    assert "cs.conversation_id = fc.first_conversation_id" in query
    assert "cs.atencion" in query
    # COALESCE: no pisar un attention bueno con NULL cuando la sesion aun no tiene score
    assert "attention = COALESCE(EXCLUDED.attention, player_conversions.attention)" in query
    # re-engagement (PIEZA 5): "convirtió a jugador" = VOLVIÓ = tiene >= 2 sesiones.
    # CTE sessions_per_contact desde conversation_sessions JOIN tickets, agrupado por
    # persona; n_sessions = count(DISTINCT session_id); return_session_id = 2da sesión.
    assert "sessions_per_contact" in query
    assert "conversation_sessions" in query
    assert "count(DISTINCT cs.session_id)" in query
    assert "(array_agg(cs.session_id ORDER BY cs.start_at))[2]" in query
    # returned es un HECHO determinista -> se pisa con EXCLUDED (sin COALESCE)
    assert "returned = EXCLUDED.returned" in query
    assert "return_session_id = EXCLUDED.return_session_id" in query
    assert "coalesce(spc.n_sessions, 0) > 1" in query


# --- EL GRANO CAMBIO Y ESTA CONSULTA NO SE ENTERO (2026-09-01) --------------
#
# BUG DE PRODUCCION:
#
#     [worker] conversión datos error: CardinalityViolation: ON CONFLICT DO UPDATE
#     command cannot affect row a second time
#
# El upsert tiene `ON CONFLICT (account, contact_id)`, y `conversation_scores` dejo de ser
# UNA fila por conversacion el 2026-08-27: ahora es una POR INTERACCION. El
# `LEFT JOIN conversation_scores ON conversation_id = first_conversation_id` abria cada
# contacto en tantas filas como interacciones tuviera su conversacion de entrada, y
# Postgres rechaza el comando entero cuando dos filas de la MISMA sentencia chocan en la
# clave del conflicto.
#
# MEDIDO sobre la copia: **526 conversaciones tienen mas de una nota**, la peor 167, y en
# total generan **5.462 filas duplicadas**. No fallo antes porque hace falta que la
# conversacion de ENTRADA de alguien junte una segunda interaccion calificada; el pase de
# conversion corre cada ~30 min y el backlog fue llegando.
#
# `first_op`, en la misma consulta, ya resolvia exactamente esto con `DISTINCT ON`. La
# leccion es de [[probar-la-costura]]: cambiar el grano de una tabla obliga a revisar a
# TODOS los que la leen, no solo a los que la escriben.

def test_el_join_con_los_scores_NO_puede_multiplicar_filas():
    """Cualquier lectura de `conversation_scores` acá tiene que venir deduplicada."""
    from src.conversions import _REFRESH_SQL
    sql = " ".join(_REFRESH_SQL.split())
    assert "ON CONFLICT (account, contact_id)" in sql
    # el join directo es el bug: tiene que pasar por un CTE con DISTINCT ON
    assert "LEFT JOIN conversation_scores" not in sql, \
        "join directo a conversation_scores: multiplica filas por interacción"
    assert "DISTINCT ON (conversation_id) conversation_id, atencion" in sql


def test_el_score_elegido_es_DETERMINISTA():
    """Sin un orden total, dos corridas eligen atenciones distintas para el mismo contacto
    y el tablero cambia solo. `interaccion_id` desempata."""
    from src.conversions import _REFRESH_SQL
    sql = " ".join(_REFRESH_SQL.split())
    assert "ORDER BY conversation_id, (atencion IS NULL), interaccion_ini, interaccion_id" in sql
