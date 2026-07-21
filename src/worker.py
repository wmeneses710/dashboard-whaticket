"""Worker de scoring: puntua conversaciones PENDIENTES de una cuenta.

Reutilizable por el batch manual (scripts/run_scoring.py) y por el loop en
background del contenedor (src/app.py). Idempotente: solo toma conversaciones
que todavia no estan en conversation_scores. Scopeado por cuenta: datos y
sistemas conviven en la misma BD y el worker procesa las cuentas configuradas.
"""
from __future__ import annotations

import time

from src.context import fetch_messages, fetch_session_messages, fetch_thread_context
from src.deposits import deposit_candidate_count
from src.llm import OllamaClient
from src.metrics import message_stats, primary_operator
from src.operators import build_operator_map, operator_name
from src.router import decide_eligibility, decide_rubric
from src.scorer import score_by_motivo, score_conversation
from src.sessions import evaluate_session
from src.store import (
    build_score_record,
    ensure_scores_columns,
    ensure_session_scoring_migration,
    fix_acquisition_ratings,
    upsert_score,
)

_CONV_FIELDS = """c.id, c.account, c.ticket_id, c.user_id, c.created_at,
       c.first_sent_message_at, c.resolved_at, c.is_new_contact,
       q.name AS queue_name, conn.channel AS channel"""

PENDING_SQL = f"""
SELECT {_CONV_FIELDS}
  FROM conversations c
  LEFT JOIN queues q         ON q.id    = c.queue_id
  LEFT JOIN connections conn ON conn.id = c.connection_id
 WHERE c.resolved_at IS NOT NULL AND c.account = %(account)s
   AND NOT EXISTS (SELECT 1 FROM conversation_scores s WHERE s.conversation_id = c.id)
 ORDER BY c.created_at DESC
 LIMIT %(limit)s
"""


def fetch_pending(cur, account: str, limit: int) -> list[dict]:
    """Conversaciones resueltas de la cuenta que aun NO fueron scoreadas."""
    cur.execute(PENDING_SQL, {"account": account, "limit": limit})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def score_and_store(conn, conv: dict, llm, op_map: dict):
    """Scorea UNA conversacion y la persiste. Devuelve (eval_status, skip_reason, score)."""
    with conn.cursor() as cur:
        msgs = fetch_messages(cur, conv["id"])
        ctx = fetch_thread_context(cur, conv["ticket_id"], conv["id"])
    stats = message_stats(msgs)
    deposit_count = deposit_candidate_count(msgs)  # gate determinista (independiente del eval_status)
    operator_id = primary_operator(msgs)
    op_name = (op_map.get(str(operator_id)) if operator_id else None) or operator_name(msgs, operator_id)
    rubric = decide_rubric(
        agent_message_count=stats.agent_message_count,
        bot_message_count=stats.bot_message_count,
    )
    eval_status, skip_reason = decide_eligibility(
        real_message_count=stats.message_count,
        customer_message_count=stats.contact_message_count,
        business_message_count=stats.agent_message_count + stats.bot_message_count,
        customer_text_count=stats.contact_text_message_count,
    )
    score = None
    if eval_status == "evaluated":
        score = score_conversation(
            rubric=rubric, target_messages=msgs, thread_context=ctx, llm=llm
        )
    record = build_score_record(
        conversation=conv, stats=stats, rubric=rubric,
        eval_status=eval_status, skip_reason=skip_reason, score=score,
        operator_id=operator_id, operator_name=op_name, deposit_count=deposit_count,
    )
    with conn.cursor() as cur:
        upsert_score(cur, record)
    conn.commit()
    return eval_status, skip_reason, score


def score_batch(conn, llm, account: str, limit: int, op_map: dict | None = None) -> dict:
    """Scorea un lote de pendientes de una cuenta. Devuelve conteos."""
    if op_map is None:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)
    with conn.cursor() as cur:
        pending = fetch_pending(cur, account, limit)
    counts = {"evaluated": 0, "skipped": 0, "error": 0, "seen": len(pending)}
    for conv in pending:
        try:
            eval_status, _, _ = score_and_store(conn, conv, llm, op_map)
            counts[eval_status] += 1
        except Exception:  # noqa: BLE001 - no abortar el lote por una conversacion
            # rollback: si el fallo fue DB-side, la txn queda abortada y cascadearia
            # al resto del lote (InFailedSqlTransaction) sin este reset.
            conn.rollback()
            counts["error"] += 1
    return counts


# --- Scoring por SESION -----------------------------------------------------------
# Espeja el path por-conversacion (PENDING_SQL/fetch_pending/score_and_store/
# score_batch) pero a grano SESION. DECISION A: una sesion se scorea solo cuando
# CERRO = su ultimo episodio quedo atras hace mas de 6h (end_at < now() - 6h). La
# fila resultante queda keyeada por conversation_id = session_id (la conversacion de
# ENTRADA, el primer episodio de la sesion) con la columna session_id seteada.
# run_worker_loop YA usa este path (el flip se hizo). El path por-conversacion
# (PENDING_SQL/fetch_pending/score_and_store/score_batch) queda como API para el batch
# manual (scripts/), pero el loop del contenedor scorea por sesion.
PENDING_SESSIONS_SQL = f"""
SELECT {_CONV_FIELDS}, cs.session_id AS session_id
  FROM conversation_sessions cs
  JOIN conversations c       ON c.id    = cs.session_id
  LEFT JOIN queues q         ON q.id    = c.queue_id
  LEFT JOIN connections conn ON conn.id = c.connection_id
 WHERE cs.account = %(account)s
   AND cs.end_at < now() - interval '6 hours'
   -- Pendiente = sin score, O con un score MAS VIEJO que el ultimo episodio de la
   -- sesion (la sesion crecio despues de scorearse, p. ej. una continuacion diferida
   -- que se mergeo hasta 48h despues) -> re-scorear para no quedar con nota vieja.
   AND NOT EXISTS (
     SELECT 1 FROM conversation_scores s
      WHERE s.session_id = cs.session_id AND s.scored_at >= cs.end_at)
 ORDER BY cs.end_at DESC, cs.session_id
 LIMIT %(limit)s
"""


def fetch_pending_sessions(cur, account: str, limit: int) -> list[dict]:
    """Sesiones CERRADAS de la cuenta que aun NO fueron scoreadas por sesion.

    Trae los campos de la conversacion de ENTRADA (mismos que _CONV_FIELDS) + el
    session_id. La fila resultante alimenta score_session_and_store, que la keyea por
    conversation_id = session_id.
    """
    cur.execute(PENDING_SESSIONS_SQL, {"account": account, "limit": limit})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def score_session_and_store(conn, sess: dict, llm, op_map: dict):
    """Scorea UNA sesion (transcript mergeado) y la persiste. Devuelve (eval_status,
    skip_reason, score). Espeja score_and_store pero a grano SESION."""
    with conn.cursor() as cur:
        msgs = fetch_session_messages(cur, sess["session_id"])
    stats, rubric, eval_status, skip_reason = evaluate_session(msgs)
    deposit_count = deposit_candidate_count(msgs)  # gate determinista (indep. del eval_status)
    operator_id = primary_operator(msgs)
    op_name = (op_map.get(str(operator_id)) if operator_id else None) or operator_name(msgs, operator_id)
    score = None
    if eval_status == "evaluated":
        # Pase v2: el LLM clasifica el MOTIVO y califica en 2 capas. thread_context
        # vacio: la sesion YA mergea todos los episodios del ticket. deposit_hint pasa
        # la senal determinista de comprobante para anclar el motivo 'deposito'.
        score = score_by_motivo(
            target_messages=msgs, thread_context="", llm=llm,
            deposit_hint=deposit_count > 0,
        )
    # El motivo (clasificado por el LLM) queda como rubrica de la fila evaluada; en las
    # skipped no hay motivo -> cae al rubric legacy (human/bot) de evaluate_session.
    record = build_score_record(
        conversation=sess, stats=stats, rubric=(score.motivo if score else rubric),
        eval_status=eval_status, skip_reason=skip_reason, score=score,
        operator_id=operator_id, operator_name=op_name, deposit_count=deposit_count,
        session_id=sess["session_id"],
    )
    with conn.cursor() as cur:
        upsert_score(cur, record)
    conn.commit()
    return eval_status, skip_reason, score


def score_sessions_batch(conn, llm, account: str, limit: int, op_map: dict | None = None) -> dict:
    """Scorea un lote de sesiones pendientes de una cuenta. Devuelve conteos."""
    if op_map is None:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)
    with conn.cursor() as cur:
        pending = fetch_pending_sessions(cur, account, limit)
    counts = {"evaluated": 0, "skipped": 0, "error": 0, "seen": len(pending)}
    for sess in pending:
        try:
            eval_status, _, _ = score_session_and_store(conn, sess, llm, op_map)
            counts[eval_status] += 1
        except Exception:  # noqa: BLE001 - no abortar el lote por una sesion
            # rollback: si el fallo fue DB-side, la txn queda abortada y cascadearia
            # al resto del lote (InFailedSqlTransaction) sin este reset.
            conn.rollback()
            counts["error"] += 1
    return counts


# Cada cuánto refrescar la tabla de conversión (determinista, sin LLM). No es por
# ciclo: es un recompute full-scale, alcanza cada tanto (el histórico cambia lento).
_CONV_REFRESH_SECONDS = 1800


def run_worker_loop(cfg, should_stop=None, log=print) -> None:
    """Loop continuo del contenedor: scorea pendientes por cuenta, duerme, repite."""
    import psycopg

    from src.conversions import refresh_account_conversions
    from src.sessions import refresh_account_sessions

    def emit(msg):
        """Log con timestamp (para leer la hora y el ritmo del goteo en prod)."""
        log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token, timeout=180.0)
    emit(f"[worker] iniciado · cuentas={cfg.scoring_accounts} batch={cfg.scoring_batch_size}")
    ok, msg = llm.check_model()  # pre-flight: no aborta, pero avisa fuerte si falta el modelo
    emit(f"[worker] {'preflight ok' if ok else 'PREFLIGHT FALLIDO'}: {msg}")
    # Migración AUTOMÁTICA a scoring por SESIÓN (una vez, antes de tocar columnas):
    # renombra la tabla vieja a backup y crea la fresca de grano sesión. Idempotente.
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                r = ensure_session_scoring_migration(cur)
            conn.commit()
        emit(f"[worker] migración: {r}")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] migración error: {type(e).__name__}: {e}")
    # Self-healing de columnas del pase LLM unificado (una vez, aditivo). La tabla de
    # prod ya existe; el CREATE ... IF NOT EXISTS no agrega columnas.
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                ensure_scores_columns(cur)
            conn.commit()
        emit("[worker] ensure_scores_columns ok")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] ensure_scores_columns error: {type(e).__name__}: {e}")
    # Opción B (una vez, por cuenta): corrige filas de conversation_scores que el
    # backfill YA escribió con rating de soporte en sesiones de adquisición (contacto
    # nuevo + segmento jugador). SQL puro, sin LLM; idempotente (ver fix_acquisition_ratings).
    for account in cfg.scoring_accounts:
        try:
            with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    n = fix_acquisition_ratings(cur, account)
                conn.commit()
            emit(f"[worker] fix acquisition ratings {account}: {n} filas")
        except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
            emit(f"[worker] fix acquisition ratings {account} error: {type(e).__name__}: {e}")
    # Sesionización inicial (una vez, antes del loop): asegura que el PRIMER ciclo tenga
    # sesiones para scorear (score_sessions_batch lee conversation_sessions).
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            for account in cfg.scoring_accounts:
                with conn.cursor() as cur:
                    s = refresh_account_sessions(cur, account)
                conn.commit()
                emit(f"[worker] sesiones iniciales {account}: {s} sesiones")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] sesiones iniciales error: {type(e).__name__}: {e}")
    last_conv = 0.0  # 0 -> corre en el primer ciclo (al arrancar)
    while not (should_stop and should_stop()):
        seen = 0
        llm_before = dict(llm.calls)  # snapshot para el delta fast/fallback del ciclo
        try:
            with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    op_map = build_operator_map(cur)
                for account in cfg.scoring_accounts:
                    t0 = time.time()
                    c = score_sessions_batch(conn, llm, account, cfg.scoring_batch_size, op_map)
                    dt = time.time() - t0
                    seen += c["seen"]
                    if c["seen"]:
                        rate = (c["evaluated"] / dt * 60) if dt > 0 else 0.0
                        emit(f"[worker] {account}: eval={c['evaluated']} skip={c['skipped']} "
                             f"err={c['error']} · {dt:.0f}s ({rate:.1f} eval/min)")
                # Pase de conversión (determinista): cada ~30min, no cada ciclo.
                if time.time() - last_conv >= _CONV_REFRESH_SECONDS:
                    for account in cfg.scoring_accounts:
                        try:
                            with conn.cursor() as cur:
                                n = refresh_account_conversions(cur, account)
                            conn.commit()
                            emit(f"[worker] conversión {account}: {n} personas")
                        except Exception as e:  # noqa: BLE001
                            conn.rollback()
                            emit(f"[worker] conversión {account} error: {type(e).__name__}: {e}")
                        # Sesionización (determinista, grano sesión): aditivo, no toca el scoring.
                        try:
                            with conn.cursor() as cur:
                                s = refresh_account_sessions(cur, account)
                            conn.commit()
                            emit(f"[worker] sesiones {account}: {s} sesiones")
                        except Exception as e:  # noqa: BLE001
                            conn.rollback()
                            emit(f"[worker] sesiones {account} error: {type(e).__name__}: {e}")
                    last_conv = time.time()
        except Exception as e:  # noqa: BLE001 - un fallo de red/DB no debe matar el loop
            emit(f"[worker] error de ciclo: {type(e).__name__}: {e}")
        # Delta LLM del ciclo: cuanto se resolvio por camino rapido vs fallback lento
        # (fallback alto = el modelo no devuelve el JSON al primer intento -> mas costo).
        d_fast = llm.calls["fast"] - llm_before["fast"]
        d_fb = llm.calls["fallback"] - llm_before["fallback"]
        d_empty = llm.calls["empty"] - llm_before["empty"]
        if d_fast or d_fb or d_empty:
            emit(f"[worker] llm ciclo: fast={d_fast} fallback={d_fb} empty={d_empty}")
        if seen == 0:  # nada pendiente -> goteo en calma; heartbeat y dormir en tramos
            emit(f"[worker] sin pendientes · durmiendo {cfg.scoring_poll_seconds}s")
            for _ in range(max(1, cfg.scoring_poll_seconds)):
                if should_stop and should_stop():
                    break
                time.sleep(1)
    emit("[worker] detenido")
