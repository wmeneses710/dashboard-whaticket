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


# LA RECONCILIACION TIENE QUE COMPARAR LA MISMA PUERTA. Hallado el 2026-08-12 auditando el
# rescore v5: `deposit_count` sale de `deposit_candidate_count`, que exige que el CLIENTE
# escriba una palabra de recarga -- exactamente lo que la puerta 2 de `es_transaccion`
# (el operador acusa el comprobante) existe para NO exigir. Asi que todo deposito que entra
# por la puerta 2 tiene `deposit_count=0` por construccion y el flag de discrepancia
# disparaba SIEMPRE, sin que hubiera discrepancia real.
# TAMAÑO MEDIDO: 889 de 2.200 filas de `deposito` (40,4%), y el 100% de ellas con
# `deposit_count=0`. La NOTA estaba bien; el indicador del dashboard mentia.
# `deposit_gate` deja pasar la respuesta de las DOS puertas; sin el, se degrada al criterio
# viejo (`deposit_count > 0`) para no romper el path por conversacion.

def test_deposit_mismatch_usa_el_gate_de_las_dos_puertas():
    # Puerta 2: el cliente manda la imagen sin texto y el operador acusa el comprobante.
    # deposit_count=0 pero el deposito EXISTE -> no hay discrepancia que reportar.
    r = _record(score=_score(deposit_observed=True), deposit_count=0, deposit_gate=True)
    assert r["deposit_mismatch"] is False


def test_deposit_mismatch_con_gate_sigue_marcando_la_discrepancia_real():
    # El gate dice que no hay deposito y el LLM dice que si: eso SI es para revisar.
    r = _record(score=_score(deposit_observed=True), deposit_count=0, deposit_gate=False)
    assert r["deposit_mismatch"] is True


def test_sin_gate_se_degrada_al_criterio_viejo():
    r = _record(score=_score(deposit_observed=True), deposit_count=0)
    assert r["deposit_mismatch"] is True


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


def test_adquisicion_ahora_se_califica_por_motivo():
    # v2: se RETIRÓ la supresión Opción B. Una sesión de adquisición (contacto nuevo,
    # jugador) con motivo promo/registro ahora SÍ lleva rating (por su rúbrica).
    r = _record(conversation={**CONV, "is_new_contact": True}, score=_score_v2("promo"))
    assert r["motivo"] == "promo"
    assert r["rating_label"] == "buena"
    assert r["stars"] == 4
    assert r["dimensions"] is not None
    assert r["rating_applicable"] is True


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



# --- rating_applicable: LEGACY de Opción B (retirada en v2). Toda fila scoreada la
# lleva en true; se conserva la columna por compatibilidad. Adquisición ahora se
# califica por su motivo (ver test_adquisicion_ahora_se_califica_por_motivo).


def test_retorno_con_score_lleva_rating_normal():
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


def test_skipped_rating_applicable_true_sin_rating():
    # v2: rating_applicable ya no distingue adquisición; queda true. La fila skipped
    # simplemente no tiene rating (sin score).
    r = _record(
        conversation={**CONV, "is_new_contact": True},
        eval_status="skipped", skip_reason="no_customer_reply", score=None,
    )
    assert r["rating_applicable"] is True
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




# `deposit_mismatch` reconcilia el GATE DETERMINISTA contra la OBSERVACION DEL LLM: es una
# señal de calidad de DATO entre dos fuentes distintas. Una rubrica determinista no tiene
# opinion que reconciliar, asi que tiene que reportar `deposit_observed=None` (= "no observo")
# y no un booleano — si no, el flag compara el gate contra un DEFAULT y dispara al vacio.
# MEDIDO el 2026-08-12 sobre la corrida v6: de 48 mismatches, **28 eran falsos** —
# 20 de `determinista/retiro-v1` (que tenia `False` hardcodeado) y 8 de
# `determinista/registro-v1` (cuyo `convirtio` quedo VENTANEADO por interaccion mientras el
# gate sigue mirando la sesion entera: ventanas distintas, mismatch sistematico).
# `promo`, `info`, `soporte` y `agilidad` ya lo hacian bien con None.

def test_las_rubricas_que_no_observan_depositos_reportan_None():
    from src.registro import score_registro
    from src.retiro import score_retiro
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

    def _m(seg, from_me, body, media="chat"):
        return {"created_at": base + timedelta(seconds=seg), "from_me": from_me,
                "is_note": False, "body": body, "media_type": media,
                "sent_from": "OPERATOR" if from_me else None}

    r = score_retiro([_m(0, False, "Monto a retirar: 30 Cedula: 0951964055 Banco: Guayaquil"),
                      _m(60, True, "Tu retiro está en proceso"), _m(180, True, "", "image")])
    assert r is not None and r.deposit_observed is None, "retiro no observa depositos"

    g = score_registro([_m(0, False, "Nancy Toaquiza toaquizanancy68@gmail.com 0986987466"),
                        _m(120, True, "Usuario: nancy593 Clave: 12345")])
    assert g is not None and g.deposit_observed is None, "registro no observa depositos"


def test_sin_observacion_no_hay_mismatch_que_reportar():
    # Es el efecto: con `deposit_observed=None`, `_deposit_mismatch` devuelve None y el flag
    # no ensucia el KPI con filas donde no habia nada que reconciliar.
    r = _record(score=_score(deposit_observed=None), deposit_count=0, deposit_gate=True)
    assert r["deposit_mismatch"] is None
