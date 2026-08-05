#!/usr/bin/env python3
"""Guarda las evaluaciones actuales en la tabla de referencia y/o vacia la tabla viva.

PARA QUE. Cada vez que se cambian los parametros de evaluacion hay que re-scorear de cero,
y antes conviene congelar lo que hay para poder comparar "antes vs despues". El mecanismo
original (ensure_session_scoring_migration) hacia esto con un RENAME, pero es de UN SOLO USO:
esta gateado por la existencia del backup, asi que ya no vuelve a mover nada. Este script
ocupa ese lugar, explicito y repetible.

    python scripts/snapshot_scores.py --dry-run              # mostrar y salir
    python scripts/snapshot_scores.py --guardar              # referencia <- copia de la actual
    python scripts/snapshot_scores.py --vaciar               # TRUNCATE de la actual
    python scripts/snapshot_scores.py --guardar --vaciar     # las dos, en UNA transaccion

POR QUE DROP + CREATE Y NO INSERT. La tabla de referencia quedo con el esquema viejo (le
faltan session_id, motivo, atencion, deposit_observed, deposit_mismatch, rating_applicable),
asi que un INSERT ... SELECT * falla. Se recrea con la forma actual; su contenido anterior se
descarta a proposito.

DESPUES DE --vaciar: las sesiones vuelven a ser PENDIENTES solas, porque el worker las elige
con `NOT EXISTS (score con scored_at >= end_at)` y no mira scoring_version. Sin filas, todo
es pendiente. OJO: el dashboard queda EN BLANCO hasta que el backfill avance, y
player_conversions.attention conserva los valores viejos (el refresh usa COALESCE para no
perderlos), asi que el cuadro de pasividad muestra datos previos hasta que se re-scoree.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.config import load_config  # noqa: E402

VIVA = "conversation_scores"
REFERENCIA = "conversation_scores_pre_session"

# El script hace DROP y TRUNCATE: si se corre por error contra produccion, se pierde el
# scoring entero. Solo procede si el nombre de la base parece una copia.
PISTAS_DE_COPIA = ("copia", "copy", "local", "test", "dev", "stage")


def _cuenta(cur, tabla: str) -> int | None:
    cur.execute("SELECT to_regclass(%s)", (tabla,))
    if cur.fetchone()[0] is None:
        return None
    cur.execute(f"SELECT count(*) FROM {tabla}")
    return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--guardar", action="store_true",
                    help=f"recrear {REFERENCIA} como copia de {VIVA}")
    ap.add_argument("--vaciar", action="store_true", help=f"TRUNCATE {VIVA}")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--forzar-produccion", action="store_true",
                    help="saltear el guard del nombre de la base (peligroso)")
    args = ap.parse_args()
    if not (args.guardar or args.vaciar):
        ap.error("elegi --guardar, --vaciar, o las dos")

    cfg = load_config()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            bd = cur.fetchone()[0]
            es_copia = any(p in bd.lower() for p in PISTAS_DE_COPIA)
            print(f"base: {bd}")
            if not es_copia and not args.forzar_produccion:
                print(f"\nABORTADO: '{bd}' no parece una copia (se buscan "
                      f"{', '.join(PISTAS_DE_COPIA)} en el nombre).")
                print("Este script hace DROP y TRUNCATE del scoring completo.")
                print("Si de verdad es lo que queres: --forzar-produccion")
                return 2

            antes_viva = _cuenta(cur, VIVA)
            antes_ref = _cuenta(cur, REFERENCIA)
            print(f"  {VIVA:34} {antes_viva if antes_viva is not None else '(no existe)'} filas")
            print(f"  {REFERENCIA:34} {antes_ref if antes_ref is not None else '(no existe)'} filas")
            print("\nplan:")
            if args.guardar:
                print(f"  1. DROP {REFERENCIA} (se descartan sus {antes_ref} filas)")
                print(f"  2. CREATE {REFERENCIA} AS SELECT * FROM {VIVA}  -> {antes_viva} filas")
                print("  3. PK + indices para las queries de comparacion")
            if args.vaciar:
                print(f"  {'4' if args.guardar else '1'}. TRUNCATE {VIVA} "
                      f"-> las {antes_viva} sesiones vuelven a ser PENDIENTES")

            if args.dry_run:
                conn.rollback()
                print("\n--dry-run: no se ejecuto nada")
                return 0

            if args.guardar:
                cur.execute(f"DROP TABLE IF EXISTS {REFERENCIA}")
                cur.execute(f"CREATE TABLE {REFERENCIA} AS SELECT * FROM {VIVA}")
                cur.execute(f"ALTER TABLE {REFERENCIA} ADD PRIMARY KEY (conversation_id)")
                # Indices pensados para comparar antes/despues, no para servir el dashboard.
                cur.execute(f"CREATE INDEX ON {REFERENCIA} (account, motivo)")
                cur.execute(f"CREATE INDEX ON {REFERENCIA} (session_id)")
            if args.vaciar:
                cur.execute(f"TRUNCATE {VIVA}")

            despues_viva = _cuenta(cur, VIVA)
            despues_ref = _cuenta(cur, REFERENCIA)
        conn.commit()

    print("\nlisto:")
    print(f"  {VIVA:34} {antes_viva} -> {despues_viva}")
    print(f"  {REFERENCIA:34} {antes_ref} -> {despues_ref}")
    if args.vaciar:
        print("\nEl dashboard queda en blanco hasta que el backfill avance.")
        print("La pasividad de player_conversions conserva los valores viejos (COALESCE).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
