"""Backfill: rellena conversation_scores.deposit_count en filas ya scoreadas.

El worker calcula deposit_count al scorear, pero las filas viejas (scoreadas
antes de cablear el dato) quedan en NULL. Esto las completa SIN re-scorear ni
usar Ollama: reusa la MISMA deteccion determinista (src.deposits) sobre los
mensajes de cada conversacion. Idempotente.

Uso:
    python -m scripts.backfill_deposits            # aplica
    python -m scripts.backfill_deposits --dry-run  # solo cuenta
"""
from __future__ import annotations

import argparse

import psycopg

from src.config import load_config
from src.context import fetch_messages
from src.deposits import deposit_candidate_count

_PENDING = "SELECT conversation_id FROM conversation_scores WHERE deposit_count IS NULL"
_UPDATE = "UPDATE conversation_scores SET deposit_count = %(n)s WHERE conversation_id = %(cid)s"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    args = ap.parse_args()

    cfg = load_config()
    filas = con_deposito = veces = 0
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute(_PENDING)
            ids = [r[0] for r in cur.fetchall()]
        for cid in ids:
            with conn.cursor() as cur:
                n = deposit_candidate_count(fetch_messages(cur, cid))
            filas += 1
            if n > 0:
                con_deposito += 1
                veces += n
            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(_UPDATE, {"n": n, "cid": cid})
        if not args.dry_run:
            conn.commit()

    verbo = "rellenaria" if args.dry_run else "relleno"
    print(f"[backfill_deposits] {verbo} {filas} filas · con deposito: {con_deposito} · veces: {veces}")


if __name__ == "__main__":
    main()
