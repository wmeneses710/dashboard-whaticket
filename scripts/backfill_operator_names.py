"""Backfill: rellena conversation_scores.user_name donde quedo NULL.

Filas legacy (scoreadas antes de cablear el mapa global de operadores) tienen
user_name NULL aunque el operador firme '*Nombre:*' en su historial. El scorer ya
usa el mapa global de ahora en mas (src.worker); esto arregla lo viejo.

Reconstruye el mapa GLOBAL user_id -> nombre (src.operators.build_operator_map) y
actualiza las filas resolubles. Los operadores que NUNCA firman quedan NULL (el
dashboard los muestra como 'Operador sin identificar', no como un id crudo).

Uso:
    python -m scripts.backfill_operator_names            # aplica
    python -m scripts.backfill_operator_names --dry-run  # solo reporta
"""
from __future__ import annotations

import argparse

import psycopg

from src.config import load_config
from src.operators import build_operator_map

_PENDING = """
SELECT user_id, count(*) AS filas
  FROM conversation_scores
 WHERE user_name IS NULL AND user_id IS NOT NULL
 GROUP BY user_id
"""

_UPDATE = """
UPDATE conversation_scores
   SET user_name = %(name)s
 WHERE user_name IS NULL AND user_id = %(user_id)s
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    args = ap.parse_args()

    cfg = load_config()
    resueltos = filas_ok = sin_firma = 0
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)
            cur.execute(_PENDING)
            pending = cur.fetchall()

        with conn.cursor() as cur:
            for user_id, filas in pending:
                name = op_map.get(str(user_id))
                if not name:
                    sin_firma += filas
                    continue
                resueltos += 1
                filas_ok += filas
                if not args.dry_run:
                    cur.execute(_UPDATE, {"name": name, "user_id": user_id})
        if not args.dry_run:
            conn.commit()

    verbo = "resolveria" if args.dry_run else "resolvio"
    print(
        f"[backfill] operadores {verbo}: {resueltos} "
        f"(filas actualizadas: {filas_ok}) · sin firma, quedan NULL: {sin_firma}"
    )


if __name__ == "__main__":
    main()
