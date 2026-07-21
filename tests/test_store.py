"""Tests del armado del registro para conversation_scores (parte pura, sin DB)."""
import re
from datetime import datetime, timedelta, timezone

import src.store as store
from src.metrics import message_stats
from src.scorer import ScoreResult
from src.store import (
    SCORING_VERSION,
    _CREATE_SCORES_TABLE,
    build_score_record,
    ensure_scores_columns,
    ensure_session_scoring_migration,
    fix_acquisition_ratings,
    is_acquisition_pitch,
)


class _FakeCursor:
    """No ejecuta SQL; solo guarda (query, params). Igual que test_conversions."""

    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.executed.append((query, params))

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

CONV = {
    "id": "conv-1",
    "account": "sistemas",
    "ticket_id": "ticket-1",
    "queue_name": "Jugadores",
    "channel": "WHATSAPP",
    "user_id": "agente-1",
    "created_at": T0,
    "first_sent_message_at": T0 + timedelta(seconds=30),
    "resolved_at": T0 + timedelta(minutes=4),
}

MSGS = [
    {"from_me": False, "is_note": False, "body": "hola"},
    {"from_me": True, "is_note": False, "body": "te ayudo"},
]


def test_registro_evaluado_lleva_estrella_y_segmento():
    score = ScoreResult(
        rubric="human",
        dimensions={"resolucion": "ok", "errores": []},
        rating_label="buena",
        rating_rationale="resolvio bien",
        stars=4,
        llm_model="qwen3.5:4b",
        atencion="empujo",
        deposit_observed=False,
    )
    r = build_score_record(
        conversation=CONV, stats=message_stats(MSGS), rubric="human",
        eval_status="evaluated", skip_reason=None, score=score,
        operator_id="op-123", operator_name="Annel Flores",
        scoring_version="2026.07-v1",
    )
    assert r["segment"] == "jugador"          # via segments.segment_for_queue
    assert r["user_name"] == "Annel Flores"
    assert r["rubric"] == "human"
    assert r["eval_status"] == "evaluated"
    assert r["skip_reason"] is None
    assert r["stars"] == 4
    assert r["rating_label"] == "buena"
    assert r["message_count"] == 2
    assert r["bot_message_count"] == 0
    assert r["user_id"] == "op-123"           # operador reconstruido, no conversations.user_id
    assert r["first_response_seconds"] == 30
    assert r["resolution_seconds"] == 240
    assert r["was_unassigned"] is False       # conversations.user_id no era null
    assert r["stars_breakdown"]["label"] == "buena"
    assert r["is_estimate"] is True


def test_registro_lleva_deposit_count():
    r = build_score_record(
        conversation=CONV, stats=message_stats(MSGS), rubric="human",
        eval_status="evaluated", skip_reason=None, score=None,
        operator_id="op-1", deposit_count=2,
    )
    assert r["deposit_count"] == 2


def test_deposit_count_por_defecto_cero():
    r = build_score_record(
        conversation=CONV, stats=message_stats(MSGS), rubric="human",
        eval_status="skipped", skip_reason="no_customer_reply", score=None,
    )
    assert r["deposit_count"] == 0


def test_registro_salteado_no_lleva_estrella():
    r = build_score_record(
        conversation={**CONV, "user_id": None}, stats=message_stats(MSGS),
        rubric="bot", eval_status="skipped", skip_reason="no_customer_reply",
        score=None, operator_id=None, scoring_version="2026.07-v1",
    )
    assert r["rubric"] == "bot"
    assert r["eval_status"] == "skipped"
    assert r["skip_reason"] == "no_customer_reply"
    assert r["stars"] is None
    assert r["rating_label"] is None
    assert r["rating_rationale"] is None
    assert r["dimensions"] is None
    assert r["was_unassigned"] is True        # conversations.user_id era null


def _score(*, atencion="empujo", deposit_observed=False):
    return ScoreResult(
        rubric="human",
        dimensions={"resolucion": "ok", "errores": []},
        rating_label="buena",
        rating_rationale="resolvio bien",
        stars=4,
        llm_model="qwen3.5:4b",
        atencion=atencion,
        deposit_observed=deposit_observed,
    )


def _record(**kwargs):
    base = dict(
        conversation=CONV, stats=message_stats(MSGS), rubric="human",
        eval_status="evaluated", skip_reason=None,
    )
    base.update(kwargs)
    return build_score_record(**base)


def test_registro_incluye_columnas_nuevas():
    r = _record(score=None)
    for col in ("atencion", "deposit_observed", "deposit_mismatch", "session_id"):
        assert col in r


def test_atencion_y_deposit_observed_propagados_desde_score():
    r = _record(score=_score(atencion="pasivo", deposit_observed=True), deposit_count=1)
    assert r["atencion"] == "pasivo"
    assert r["deposit_observed"] is True


def test_deposit_mismatch_det_si_llm_no_es_true():
    # determinista detecta deposito (count>0) pero el LLM no lo observo -> discrepa
    r = _record(score=_score(deposit_observed=False), deposit_count=2)
    assert r["deposit_mismatch"] is True


def test_deposit_mismatch_det_no_llm_no_es_false():
    r = _record(score=_score(deposit_observed=False), deposit_count=0)
    assert r["deposit_mismatch"] is False


def test_deposit_mismatch_det_si_llm_si_es_false():
    r = _record(score=_score(deposit_observed=True), deposit_count=2)
    assert r["deposit_mismatch"] is False


def test_deposit_mismatch_sin_score_es_none():
    r = _record(score=None, deposit_count=2)
    assert r["deposit_mismatch"] is None


def test_deposit_mismatch_deposit_observed_none_es_none():
    r = _record(score=_score(deposit_observed=None), deposit_count=2)
    assert r["deposit_mismatch"] is None


def test_path_salteado_columnas_nuevas_en_none():
    r = _record(eval_status="skipped", skip_reason="no_customer_reply", score=None)
    assert r["atencion"] is None
    assert r["deposit_observed"] is None
    assert r["deposit_mismatch"] is None
    assert r["session_id"] is None


def test_session_id_pasa_al_record():
    r = _record(score=None, session_id="sess-42")
    assert r["session_id"] == "sess-42"


def _score_v2(motivo="deposito"):
    return ScoreResult(
        rubric=motivo, dimensions={"resolucion": "ok", "iniciativa": "x", "cortesia": "y", "errores": []},
        rating_label="buena", rating_rationale="ok", stars=4, llm_model="qwen3:14b",
        atencion="empujo", deposit_observed=False, motivo=motivo,
    )


def test_motivo_del_score_se_persiste():
    r = _record(conversation={**CONV, "is_new_contact": False}, score=_score_v2("retiro"))
    assert r["motivo"] == "retiro"


def test_motivo_es_none_en_skipped():
    r = _record(eval_status="skipped", skip_reason="no_customer_reply", score=None)
    assert r["motivo"] is None


def test_motivo_se_persiste_aunque_sea_adquisicion():
    # En adquisición el rating se suprime, pero el MOTIVO igual se guarda.
    r = _record(conversation={**CONV, "is_new_contact": True}, score=_score_v2("promo"))
    assert r["rating_label"] is None      # rating suprimido (Opción B)
    assert r["motivo"] == "promo"         # motivo igual persiste


def test_ensure_scores_columns_incluye_motivo():
    cur = _FakeCursor()
    ensure_scores_columns(cur)
    qs = [q for q, _ in cur.executed]
    assert any("ADD COLUMN IF NOT EXISTS" in q and "motivo" in q for q in qs)


def test_create_table_incluye_motivo():
    assert "motivo" in _CREATE_SCORES_TABLE


def test_ensure_scores_columns_emite_alters():
    cur = _FakeCursor()
    ensure_scores_columns(cur)
    qs = [q for q, _ in cur.executed]
    for col in ("atencion", "deposit_observed", "deposit_mismatch", "session_id"):
        assert any(
            "ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS" in q and col in q
            for q in qs
        ), f"falta ALTER para {col}"


# --- Migración automática "desde cero con backup" (grano sesión) --------------

class _MigrationCursor:
    """Cursor falso para la migración. `regclass` mapea nombre de tabla ->
    valor devuelto por to_regclass (None = no existe). Cada execute() de un
    SELECT to_regclass(...) prepara el fetchone() correspondiente por query."""

    def __init__(self, regclass: dict):
        self._regclass = regclass
        self.executed = []
        self._next = None

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if "to_regclass" in query:
            m = re.search(r"to_regclass\('([^']+)'\)", query)
            name = m.group(1)
            self._next = (self._regclass.get(name),)
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return []  # sin indices en el unit test; el rename de indices se valida en la copia

    def queries(self):
        return [q for q, _ in self.executed]


def _has(cur, needle):
    return any(needle in q for q in cur.queries())


def test_migracion_backup_ausente_tabla_vieja_presente_renombra_y_crea_fresca():
    cur = _MigrationCursor({
        "conversation_scores_pre_session": None,       # backup NO existe
        "conversation_scores": "conversation_scores",  # tabla vieja SI existe
    })
    result = ensure_session_scoring_migration(cur)
    assert result == {"migrated": True}
    assert _has(cur, "ALTER TABLE conversation_scores RENAME TO conversation_scores_pre_session")
    assert _has(cur, "CREATE TABLE IF NOT EXISTS conversation_scores")


def test_migracion_backup_presente_no_renombra_pero_asegura_fresca():
    cur = _MigrationCursor({
        "conversation_scores_pre_session": "conversation_scores_pre_session",  # ya migrado
        "conversation_scores": "conversation_scores",
    })
    result = ensure_session_scoring_migration(cur)
    assert result == {"migrated": False}
    assert not _has(cur, "RENAME TO")           # NO re-renombra (no destruye)
    assert _has(cur, "CREATE TABLE IF NOT EXISTS conversation_scores")


def test_migracion_instalacion_nueva_sin_backup_ni_tabla_vieja_solo_crea_fresca():
    cur = _MigrationCursor({
        "conversation_scores_pre_session": None,  # sin backup
        "conversation_scores": None,              # sin tabla vieja (install nueva)
    })
    result = ensure_session_scoring_migration(cur)
    assert result == {"migrated": False}         # no había nada que respaldar
    assert not _has(cur, "RENAME TO")
    assert _has(cur, "CREATE TABLE IF NOT EXISTS conversation_scores")


def test_migracion_crea_indice_por_session_id():
    cur = _MigrationCursor({
        "conversation_scores_pre_session": None,
        "conversation_scores": None,
    })
    ensure_session_scoring_migration(cur)
    assert _has(cur, "CREATE INDEX IF NOT EXISTS")
    assert _has(cur, "(session_id)")


def test_migracion_idempotente_segunda_corrida_no_renombra():
    # Primera corrida: migra. Segunda corrida (backup ya presente): no toca nada.
    cur1 = _MigrationCursor({
        "conversation_scores_pre_session": None,
        "conversation_scores": "conversation_scores",
    })
    assert ensure_session_scoring_migration(cur1) == {"migrated": True}
    cur2 = _MigrationCursor({
        "conversation_scores_pre_session": "conversation_scores_pre_session",
        "conversation_scores": "conversation_scores",
    })
    assert ensure_session_scoring_migration(cur2) == {"migrated": False}
    assert not _has(cur2, "RENAME TO")


def test_scoring_version_bumped_a_session_v2():
    assert SCORING_VERSION == "2026.07-session-v2"


# --- Opción B: adquisición (contacto nuevo, segmento jugador) no lleva rating ----
# de SOPORTE (mide "¿resolviste el problema?"), porque un pitch de venta no es
# eso. Sigue llevando atencion/deposit_observed (esos SÍ son la métrica correcta).


def test_is_acquisition_pitch_true_si_nuevo_y_jugador():
    assert is_acquisition_pitch({"is_new_contact": True}, "jugador") is True


def test_is_acquisition_pitch_false_si_no_es_contacto_nuevo():
    # retorno (recarga/retiro) del mismo contacto: transaccional, la rúbrica lo maneja bien
    assert is_acquisition_pitch({"is_new_contact": False}, "jugador") is False


def test_is_acquisition_pitch_false_si_segmento_no_es_jugador():
    assert is_acquisition_pitch({"is_new_contact": True}, "agente") is False


def test_is_acquisition_pitch_false_sin_is_new_contact():
    assert is_acquisition_pitch({}, "jugador") is False


def test_adquisicion_con_score_no_lleva_rating_pero_si_atencion_y_deposito():
    score = _score(atencion="empujo", deposit_observed=True)
    r = _record(
        conversation={**CONV, "is_new_contact": True}, score=score, deposit_count=1,
    )
    assert r["dimensions"] is None
    assert r["rating_label"] is None
    assert r["rating_rationale"] is None
    assert r["stars"] is None
    assert r["atencion"] == "empujo"        # SÍ aplica a adquisición
    assert r["deposit_observed"] is True    # SÍ aplica a adquisición
    assert r["rating_applicable"] is False


def test_retorno_con_score_lleva_rating_normal_como_hoy():
    score = _score()
    r = _record(conversation={**CONV, "is_new_contact": False}, score=score)
    assert r["rating_label"] == "buena"
    assert r["stars"] == 4
    assert r["dimensions"] is not None
    assert r["rating_applicable"] is True


def test_no_jugador_con_score_lleva_rating_normal_aunque_sea_contacto_nuevo():
    other_queue_conv = {**CONV, "queue_name": "Agente", "is_new_contact": True}
    score = _score()
    r = _record(conversation=other_queue_conv, score=score)
    assert r["segment"] == "agente"
    assert r["rating_label"] == "buena"
    assert r["rating_applicable"] is True


def test_adquisicion_skipped_rating_applicable_false_igual():
    r = _record(
        conversation={**CONV, "is_new_contact": True},
        eval_status="skipped", skip_reason="no_customer_reply", score=None,
    )
    assert r["rating_applicable"] is False
    assert r["rating_label"] is None


def test_create_table_incluye_rating_applicable():
    assert "rating_applicable" in _CREATE_SCORES_TABLE


def test_ensure_scores_columns_incluye_rating_applicable():
    cur = _FakeCursor()
    ensure_scores_columns(cur)
    qs = [q for q, _ in cur.executed]
    assert any(
        "ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS" in q
        and "rating_applicable" in q
        for q in qs
    ), "falta ALTER para rating_applicable"


# --- fix_acquisition_ratings: corrección de una sola pasada (SQL puro, sin LLM) --


def test_fix_acquisition_ratings_qids_vacio_no_ejecuta_update(monkeypatch):
    monkeypatch.setattr(store, "_jugador_queue_ids", lambda cur, account: [])
    cur = _FakeCursor()
    n = fix_acquisition_ratings(cur, "sistemas")
    assert n == 0
    assert not any("UPDATE conversation_scores" in q for q, _ in cur.executed)


def test_fix_acquisition_ratings_arma_el_update_correcto(monkeypatch):
    monkeypatch.setattr(store, "_jugador_queue_ids", lambda cur, account: ["q1", "q2"])
    cur = _FakeCursor()
    cur.rowcount = 5
    n = fix_acquisition_ratings(cur, "sistemas")
    assert n == 5
    upd = [(q, p) for q, p in cur.executed if "UPDATE conversation_scores" in q]
    assert len(upd) == 1
    query, params = upd[0]
    assert "SET dimensions=NULL" in query or "SET dimensions = NULL" in query
    # stars_breakdown es JSONB derivado de rating_label/stars; si no se limpia junto,
    # el rating manchado quedaria filtrando por ahi aunque rating_label sea NULL.
    assert "stars_breakdown=NULL" in query or "stars_breakdown = NULL" in query
    assert "rating_applicable=false" in query or "rating_applicable = false" in query
    assert "c.is_new_contact" in query
    assert "c.queue_id = ANY(%(qids)s)" in query
    assert "rating_applicable IS DISTINCT FROM false" in query
    assert "cs.account = %(account)s" in query
    assert params == {"account": "sistemas", "qids": ["q1", "q2"]}


def test_fix_acquisition_ratings_es_idempotente_por_el_guard(monkeypatch):
    # el guard IS DISTINCT FROM false evita re-tocar filas ya corregidas: se valida
    # con la presencia del guard en el SQL (la corrección real es contra la copia).
    monkeypatch.setattr(store, "_jugador_queue_ids", lambda cur, account: ["q1"])
    cur = _FakeCursor()
    fix_acquisition_ratings(cur, "datos")
    query, _ = cur.executed[-1]
    assert "rating_applicable IS DISTINCT FROM false" in query
