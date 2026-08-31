#!/usr/bin/env python3
"""Encola un rescore PARCIAL: marca las filas que un cambio toca, y solo esas.

POR QUE EXISTE. Hasta el 2026-08-31 rescorear era TRUNCAR la tabla: 146.968 sesiones a
16,6 por hora son ~369 dias, y mientras tanto el tablero muestra el 0,7% de la historia.
Un arreglo tipico toca 77 filas (el falso negativo de "esta listo") o 65 (v24). Se pagaban
369 dias por 77 filas.

COMO FUNCIONA. Pone `rescore_pedido_at = now()` en las filas que elijas. El worker las
vuelve a encolar porque `PENDING_SESSIONS_SQL` tiene una rama para eso, y quedan servidas
cuando `scored_at >= rescore_pedido_at`. No hay flag que apagar despues.

EL SISTEMA NO ADIVINA QUE FILAS TOCA UN CAMBIO, y no puede: uno en la rubrica de `retiro`
toca `motivo='retiro'`, pero uno en `client_sin_motivo` toca cualquier cosa. La condicion
la escribe quien hizo el cambio. Lo que SI hace esta herramienta es no dejarte marcar a
ciegas: primero cuenta, y recien escribe si se lo pedis con `--aplicar`.

OJO CON EL GRANO. El worker scorea por SESION: marcar una interaccion rescorea su sesion
ENTERA. Por eso el conteo muestra las dos cifras -- filas y sesiones -- y en una sesion de
167 interacciones esa diferencia es la que vas a pagar.

    # 1) mirar (no escribe nada)
    python scripts/pedir_rescore.py "motivo = 'deposito' AND scoring_version < '2026.09-v25'"

    # 2) aplicar
    python scripts/pedir_rescore.py "motivo = 'deposito'" --aplicar

    # deshacer lo ultimo que marcaste, si te arrepentiste antes de que corra el worker
    python scripts/pedir_rescore.py --deshacer --aplicar
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

# Sin condicion no se marca NADA. Un `WHERE true` accidental encola la tabla entera y eso
# es exactamente el rescore de 369 dias que este script viene a evitar.
_PROHIBIDAS = ("true", "1=1", "")


def _contar(cur, condicion: str) -> tuple[int, int]:
    cur.execute(
        f"SELECT count(*), count(DISTINCT conversation_id) "  # noqa: S608 - condicion del operador
        f"FROM conversation_scores WHERE {condicion}")
    return cur.fetchone()


def _muestra(cur, condicion: str, n: int = 8) -> list:
    cur.execute(
        f"SELECT motivo, stars, user_name, scoring_version, scored_at "  # noqa: S608
        f"FROM conversation_scores WHERE {condicion} ORDER BY scored_at DESC LIMIT {n}")
    return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("condicion", nargs="?", help="el WHERE, sin la palabra WHERE")
    ap.add_argument("--aplicar", action="store_true", help="escribe (sin esto, solo cuenta)")
    ap.add_argument("--deshacer", action="store_true",
                    help="borra los pedidos que todavia NO fueron servidos")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    a = ap.parse_args(argv)

    if not a.dsn:
        print("falta DATABASE_URL (o --dsn)", file=sys.stderr)
        return 2
    if a.deshacer:
        # Solo lo NO servido: una fila ya rescoreada no tiene nada que deshacer, y borrarle
        # el pedido perderia el registro de que se pidio.
        cond = "rescore_pedido_at IS NOT NULL AND rescore_pedido_at > scored_at"
    else:
        cond = (a.condicion or "").strip().rstrip(";")
        if cond.lower() in _PROHIBIDAS:
            print("condicion vacia o universal: eso encola la tabla entera. "
                  "Si es lo que querés, truncá a mano y asumilo.", file=sys.stderr)
            return 2

    with psycopg.connect(a.dsn, connect_timeout=8) as conn, conn.cursor() as cur:
        try:
            filas, sesiones = _contar(cur, cond)
        except Exception as e:  # noqa: BLE001 - la condicion la escribe una persona
            print(f"la condicion no corre: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        print(f"condicion: {cond}")
        print(f"  filas (interacciones) ... {filas}")
        print(f"  SESIONES que se rescorean {sesiones}   <- es lo que se paga: el worker "
              f"scorea por sesion")
        if not a.deshacer and filas:
            print("  muestra:")
            for f in _muestra(cur, cond):
                print(f"    {f}")
        if not filas:
            print("nada que hacer.")
            return 0
        if not a.aplicar:
            print("\nNO se escribio nada. Volvé a correrlo con --aplicar si el numero cierra.")
            return 0

        if a.deshacer:
            cur.execute(f"UPDATE conversation_scores SET rescore_pedido_at = NULL "  # noqa: S608
                        f"WHERE {cond}")
        else:
            cur.execute(f"UPDATE conversation_scores SET rescore_pedido_at = now() "  # noqa: S608
                        f"WHERE {cond}")
        conn.commit()
        print(f"\nlisto: {cur.rowcount} filas "
              f"{'desmarcadas' if a.deshacer else 'encoladas'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
