#!/usr/bin/env python3
"""Re-materializa las sesiones de una o varias cuentas, SIN scorear.

PARA QUE. `refresh_account_sessions` solo se llamaba desde el worker (arranque + loop),
y el worker ADEMAS scorea. Este script separa los pasos, porque el orden importa:

    1. recortar mensajes viejos                  (si no, el start_at cae en otro mes)
    2. python scripts/rematerializar_sesiones.py <- ESTE
    3. python scripts/snapshot_scores.py --guardar --vaciar
    4. re-scorear (worker o batch)

Arrancar el worker antes del paso 3 scorea con la tabla vieja llena, y el snapshot
posterior congela una mezcla de dos lineas base.

    python scripts/rematerializar_sesiones.py --dry-run        # ver el estado y salir
    python scripts/rematerializar_sesiones.py                  # todas las cuentas
    python scripts/rematerializar_sesiones.py --cuenta sistemas

ESCRIBE en conversation_sessions y conversation_session_map, asi que:
  - Solo corre si el nombre de la base parece una copia (mismo guard que snapshot_scores).
  - Toma el MISMO advisory lock que el worker: si el worker esta corriendo, este script
    NO arranca. Es a proposito — los dos escribiendo conversation_sessions deadlockean
    (bug del commit 3a9e337).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.config import load_config  # noqa: E402
from src.rematerializar import (  # noqa: E402
    LOCK_KEY,
    PISTAS_DE_COPIA,
    es_copia,
    formatear_reporte,
    rematerializar,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cuenta", action="append",
                    help="cuenta a re-materializar (repetible; default SCORING_ACCOUNTS)")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostrar el estado actual y salir sin escribir")
    ap.add_argument("--forzar-produccion", action="store_true",
                    help="saltear el guard del nombre de la base (peligroso: ESCRIBE)")
    args = ap.parse_args()

    cfg = load_config()
    cuentas = tuple(args.cuenta) if args.cuenta else cfg.scoring_accounts

    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            bd = cur.fetchone()[0]
        print(f"base: {bd}   cuentas: {', '.join(cuentas)}")
        if not es_copia(bd) and not args.forzar_produccion:
            print(f"\nABORTADO: '{bd}' no parece una copia (se buscan "
                  f"{', '.join(PISTAS_DE_COPIA)} en el nombre).")
            print("Este script ESCRIBE en conversation_sessions y _map.")
            print("Si de verdad es lo que queres: --forzar-produccion")
            return 2

        # Lock compartido con el worker. Se toma en una conexion aparte en autocommit
        # para que viva mientras dura el proceso y no dependa de la transaccion de trabajo.
        lock_conn = psycopg.connect(cfg.database_url, connect_timeout=8)
        lock_conn.autocommit = True
        try:
            tengo = lock_conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]
            if not tengo:
                print("\nABORTADO: otro proceso tiene el lock de scoring (lock "
                      f"{LOCK_KEY}). Casi seguro es el worker.")
                print("Pararlo antes de re-materializar: los dos escribiendo "
                      "conversation_sessions deadlockean.")
                return 3

            resultados = []
            for cuenta in cuentas:
                with conn.cursor() as cur:
                    resultados.append(rematerializar(cur, cuenta, dry_run=args.dry_run))
                if not args.dry_run:
                    conn.commit()   # commit por cuenta: una cuenta grande no bloquea al resto
                    print(f"  [{cuenta}] listo")
        finally:
            lock_conn.close()   # libera el advisory lock

    print()
    print(formatear_reporte(resultados, dry_run=args.dry_run))
    if not args.dry_run:
        print("\nSIGUIENTE PASO: snapshot_scores.py --guardar --vaciar, y despues re-scorear.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
