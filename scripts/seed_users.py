"""Seed de la tabla `users` a partir de la firma '*Nombre:*' de los mensajes.

La `users` del dump local viene VACIA (en prod si esta poblada), asi que el
dashboard cae al COALESCE(u.name, cs.user_name). Este barrido la rellena en
local: por cada user_id que firma sus mensajes, toma el nombre mas frecuente y
la cuenta a la que pertenece, e inserta/actualiza la fila en `users`.

Usa el MISMO patron de firma que src.operators (fuente unica de verdad). Los
operadores que nunca firman no entran (siguen resolviendose por user_name).

Idempotente: ON CONFLICT (id) DO UPDATE. Correr tras cada restore local.

Uso:
    python -m scripts.seed_users            # aplica
    python -m scripts.seed_users --dry-run  # solo reporta
"""
from __future__ import annotations

import argparse

import psycopg

from src.config import load_config
from src.operators import _PG_NAME_RE

# Nombre + cuenta mas frecuentes por operador (DISTINCT ON toma el top por n).
_SWEEP = """
SELECT DISTINCT ON (user_id) user_id, account, name
  FROM (
        SELECT user_id, account,
               (regexp_match(body, %(re)s))[1] AS name,
               count(*) AS n
          FROM messages
         WHERE from_me AND NOT is_note AND user_id IS NOT NULL
           AND body ~ %(re)s
         GROUP BY user_id, account, name
       ) t
 WHERE name IS NOT NULL
 ORDER BY user_id, n DESC
"""

_UPSERT = """
INSERT INTO users (id, account, name)
VALUES (%(id)s, %(account)s, %(name)s)
ON CONFLICT (id) DO UPDATE
   SET name = EXCLUDED.name, account = EXCLUDED.account
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    args = ap.parse_args()

    cfg = load_config()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute(_SWEEP, {"re": _PG_NAME_RE})
            rows = cur.fetchall()

        if not args.dry_run:
            with conn.cursor() as cur:
                for user_id, account, name in rows:
                    cur.execute(_UPSERT, {"id": user_id, "account": account, "name": name})
            conn.commit()

    verbo = "sembraria" if args.dry_run else "sembro"
    print(f"[seed_users] operadores que {verbo}: {len(rows)}")
    for user_id, account, name in rows:
        print(f"  {user_id} [{account}] -> {name}")


if __name__ == "__main__":
    main()
