"""Worker de scoring: puntua conversaciones PENDIENTES de una cuenta.

Reutilizable por el batch manual (scripts/run_scoring.py) y por el loop en
background del contenedor (src/app.py). Idempotente: solo toma conversaciones
que todavia no estan en conversation_scores. Scopeado por cuenta: datos y
sistemas conviven en la misma BD y el worker procesa las cuentas configuradas.
"""
from __future__ import annotations

import time

from src.context import fetch_messages, fetch_thread_context
from src.llm import OllamaClient
from src.metrics import message_stats, primary_operator
from src.operators import build_operator_map, operator_name
from src.router import decide_eligibility, decide_rubric
from src.scorer import score_conversation
from src.store import build_score_record, upsert_score

_CONV_FIELDS = """c.id, c.account, c.ticket_id, c.user_id, c.created_at,
       c.first_sent_message_at, c.resolved_at, q.name AS queue_name, conn.channel AS channel"""

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
    )
    score = None
    if eval_status == "evaluated":
        score = score_conversation(
            rubric=rubric, target_messages=msgs, thread_context=ctx, llm=llm
        )
    record = build_score_record(
        conversation=conv, stats=stats, rubric=rubric,
        eval_status=eval_status, skip_reason=skip_reason, score=score,
        operator_id=operator_id, operator_name=op_name,
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
            counts["error"] += 1
    return counts


def run_worker_loop(cfg, should_stop=None, log=print) -> None:
    """Loop continuo del contenedor: scorea pendientes por cuenta, duerme, repite."""
    import psycopg

    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, timeout=180.0)
    log(f"[worker] iniciado · cuentas={cfg.scoring_accounts} batch={cfg.scoring_batch_size}")
    ok, msg = llm.check_model()  # pre-flight: no aborta, pero avisa fuerte si falta el modelo
    log(f"[worker] {'preflight ok' if ok else 'PREFLIGHT FALLIDO'}: {msg}")
    while not (should_stop and should_stop()):
        seen = 0
        try:
            with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    op_map = build_operator_map(cur)
                for account in cfg.scoring_accounts:
                    c = score_batch(conn, llm, account, cfg.scoring_batch_size, op_map)
                    seen += c["seen"]
                    if c["seen"]:
                        log(f"[worker] {account}: eval={c['evaluated']} skip={c['skipped']} err={c['error']}")
        except Exception as e:  # noqa: BLE001 - un fallo de red/DB no debe matar el loop
            log(f"[worker] error de ciclo: {type(e).__name__}: {e}")
        if seen == 0:  # nada pendiente -> dormir en tramos para poder frenar
            for _ in range(max(1, cfg.scoring_poll_seconds)):
                if should_stop and should_stop():
                    break
                time.sleep(1)
    log("[worker] detenido")
