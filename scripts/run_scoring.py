"""Batch de scoring que PERSISTE en conversation_scores.

Flujo por conversacion (newest-first): router (elegibilidad) -> metricas
objetivas -> (si evaluated) LLM scorer -> UPSERT. La tabla es derivada y
separada del ETL: ante cualquier ajuste, TRUNCATE conversation_scores y re-run.

Uso:
    python -m scripts.run_scoring --limit 100
    python -m scripts.run_scoring --limit 100 --skip-scored   # no re-scorea
"""
from __future__ import annotations

import argparse
import time

import psycopg

from src.config import load_config
from src.llm import OllamaClient
from src.operators import build_operator_map
from src.worker import score_and_store

_SELECT = """
SELECT c.id, c.account, c.ticket_id, c.user_id, c.created_at,
       c.first_sent_message_at, c.resolved_at,
       q.name AS queue_name, conn.channel AS channel
  FROM conversations c
  LEFT JOIN queues q      ON q.id = c.queue_id
  LEFT JOIN connections conn ON conn.id = c.connection_id
 WHERE c.resolved_at IS NOT NULL
   {extra}
"""

# Newest-first: flujo vivo / backfill ordenado.
BATCH_SQL = _SELECT + " ORDER BY c.created_at DESC LIMIT %(limit)s"

# Muestra diversa para validar: mitad human, mitad bot, al azar en el tiempo.
DIVERSE_SQL = (
    "(" + _SELECT + " AND c.user_id IS NOT NULL ORDER BY random() LIMIT %(half)s)"
    " UNION ALL "
    "(" + _SELECT + " AND c.user_id IS NULL ORDER BY random() LIMIT %(half)s)"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--skip-scored", action="store_true",
                    help="no re-scorea conversaciones que ya tienen fila")
    ap.add_argument("--diverse", action="store_true",
                    help="muestra mitad human / mitad bot al azar (para validar)")
    args = ap.parse_args()

    cfg = load_config()
    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, timeout=180.0)
    extra = ("AND NOT EXISTS (SELECT 1 FROM conversation_scores s "
             "WHERE s.conversation_id = c.id)") if args.skip_scored else ""
    sql = (DIVERSE_SQL if args.diverse else BATCH_SQL).format(extra=extra)

    counts = {"evaluated": 0, "skipped": 0, "error": 0}
    t_start = time.perf_counter()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)  # user_id -> nombre (global)
            cur.execute(sql, {"limit": args.limit, "half": max(1, args.limit // 2)})
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]

        for i, row in enumerate(rows, 1):
            conv = dict(zip(cols, row))
            try:
                eval_status, skip_reason, score = score_and_store(conn, conv, llm, op_map)
            except Exception as e:  # noqa: BLE001 - no abortar el batch por una
                counts["error"] += 1
                print(f"[{i}/{len(rows)}] {str(conv['id'])[:8]} ERROR {type(e).__name__}: {e}",
                      flush=True)
                continue
            counts[eval_status] += 1
            tag = f"{score.rating_label} {score.stars}*" if score else f"skipped:{skip_reason}"
            print(f"[{i}/{len(rows)}] {str(conv['id'])[:8]} -> {tag}", flush=True)

    dt = time.perf_counter() - t_start
    print(f"\nlisto en {dt:.0f}s | evaluated={counts['evaluated']} "
          f"skipped={counts['skipped']} error={counts['error']}", flush=True)


if __name__ == "__main__":
    main()
