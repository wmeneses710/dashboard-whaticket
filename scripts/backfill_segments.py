"""Backfill: recomputa segment + queue_name en conversation_scores ya scoreadas.

Las filas scoreadas ANTES de sembrar `queues` quedaron con queue_name NULL y
segment='interno'. Ahora que las colas están, se recomputa el segmento real
(src.segments.segment_for_queue) uniendo la conversación -> queue_id -> queues.
Determinista, sin Ollama. Idempotente. Las nuevas conversaciones ya salen bien
del worker (que ahora resuelve queue_name por el JOIN a queues).

Uso:
    python -m scripts.backfill_segments            # aplica
    python -m scripts.backfill_segments --dry-run  # solo reporta
"""
from __future__ import annotations

import argparse
from collections import Counter

import psycopg

from src.config import load_config
from src.segments import segment_for_queue

_PENDING = """
SELECT cs.conversation_id, q.name
  FROM conversation_scores cs
  JOIN conversations c ON c.id = cs.conversation_id
  LEFT JOIN queues q ON q.id = c.queue_id
"""
_UPDATE = "UPDATE conversation_scores SET segment=%(seg)s, queue_name=%(qn)s WHERE conversation_id=%(cid)s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    args = ap.parse_args()

    cfg = load_config()
    dist: Counter = Counter()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute(_PENDING)
            rows = cur.fetchall()
        with conn.cursor() as cur:
            for cid, qname in rows:
                seg = segment_for_queue(qname)
                dist[seg] += 1
                if not args.dry_run:
                    cur.execute(_UPDATE, {"seg": seg, "qn": qname, "cid": cid})
        if not args.dry_run:
            conn.commit()

    verbo = "recomputaria" if args.dry_run else "recomputo"
    print(f"[backfill_segments] {verbo} {len(rows)} filas · {dict(dist)}")


if __name__ == "__main__":
    main()
