#!/usr/bin/env python3
"""Encola un rescore PARCIAL: marca las filas que un cambio toca, y solo esas.

POR QUE EXISTE. Hasta el 2026-08-31 rescorear era TRUNCAR la tabla: 146.968 sesiones a
16,6 por hora son ~369 dias, y mientras tanto el tablero muestra el 0,7% de la historia.
Un arreglo tipico toca 77 filas (el falso negativo de "esta listo") o 65 (v24). Se pagaban
369 dias por 77 filas.

COMO FUNCIONA. Pone `rescore_pedido_at = now()` en las filas que elijas. El worker las
vuelve a encolar porque `PENDING_SESSIONS_SQL` tiene una rama para eso, y quedan servidas
cuando `scored_at >= rescore_pedido_at`. No hay flag que apagar despues.

DOS FORMAS DE ELEGIR:

  POR LISTA DE IDS -- la de EasyPanel, donde hay una consola y una caja de texto y no una
  terminal con un archivo al lado. Acepta interacciones, sesiones o tickets MEZCLADOS, con
  cualquier separador (coma, espacio, salto de linea, punto y coma, pipe) y aguanta las
  comillas y corchetes de un copy-paste. Los tres son uuid y viven en columnas distintas,
  asi que se resuelven preguntando -- no hace falta que digas cual es cual.

      python scripts/pedir_rescore.py --ids "16847caa-...,bfe635f8-..."
      python scripts/pedir_rescore.py --ids-de-archivo ids.txt
      echo "$IDS" | python scripts/pedir_rescore.py --ids -
      RESCORE_IDS="16847caa-... bfe635f8-..." python scripts/pedir_rescore.py --ids-de-env

  POR CONDICION -- cuando el cambio toca una familia entera y no una lista.

      python scripts/pedir_rescore.py "motivo='deposito' AND scoring_version < '2026.09-v25'"

NADA SE ESCRIBE SIN `--aplicar`. Primero cuenta, muestra que es cada id y que NO matcheo, y
recien escribe si se lo pedis. Un id que no matchea es lo mas probable que pase (un uuid de
otra base, o de una fila que todavia no se scoreo) y tiene que VERSE: sin eso, encolas de
menos creyendo que encolaste todo.

OJO CON EL GRANO, que es lo que se paga:
    ticket (831)  --1:N-->  sesion (1.064)  --1:N-->  interaccion (4.705)
Un ticket llega a 31 sesiones y 167 interacciones. Y el worker scorea por SESION, asi que
marcar UNA interaccion rescorea todas las hermanas de su sesion. Por eso el conteo muestra
las dos cifras.

    # deshacer lo que todavia no se sirvio, si te arrepentiste antes de que corra el worker
    python scripts/pedir_rescore.py --deshacer --aplicar
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.rescore import clasificar, condicion_por_ids, estado, parse_uuids  # noqa: E402

# Sin condicion no se marca NADA. Un `WHERE true` accidental encola la tabla entera y eso
# es exactamente el rescore de 369 dias que este script viene a evitar.
_PROHIBIDAS = ("true", "1=1", "")


def _leer_ids(a) -> str:
    """El texto crudo con los ids, venga de donde venga."""
    if a.ids_de_archivo:
        with open(a.ids_de_archivo, encoding="utf-8") as f:
            return f.read()
    if a.ids_de_env:
        return os.environ.get(a.ids_de_env, "")
    if a.ids == "-":            # pipe: `echo "$IDS" | ... --ids -`
        return sys.stdin.read()
    return a.ids or ""


def _contar(cur, condicion: str, params) -> tuple[int, int]:
    cur.execute(
        f"SELECT count(*), count(DISTINCT conversation_id) "  # noqa: S608
        f"FROM conversation_scores WHERE {condicion}", params)
    return cur.fetchone()


def _muestra(cur, condicion: str, params, n: int = 8) -> list:
    cur.execute(
        f"SELECT motivo, stars, user_name, scoring_version, scored_at "  # noqa: S608
        f"FROM conversation_scores WHERE {condicion} ORDER BY scored_at DESC LIMIT {n}",
        params)
    return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("condicion", nargs="?", help="el WHERE, sin la palabra WHERE")
    ap.add_argument("--ids", help="lista de uuids (o '-' para leer de stdin)")
    ap.add_argument("--ids-de-archivo", help="archivo con los uuids")
    ap.add_argument("--ids-de-env", metavar="VAR",
                    help="variable de entorno con los uuids (p. ej. RESCORE_IDS)")
    ap.add_argument("--aplicar", action="store_true", help="escribe (sin esto, solo cuenta)")
    ap.add_argument("--deshacer", action="store_true",
                    help="borra los pedidos que todavia NO fueron servidos")
    ap.add_argument("--estado", action="store_true",
                    help="cuanto falta de la cola. SOLO LECTURA, no escribe nunca")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    a = ap.parse_args(argv)

    # ANTES DE CONECTARSE: si una consulta de estado llegara a la base con un flag de
    # escritura pegado, ya seria tarde. Preguntar como va la cola no comparte comando con
    # borrarla, y el parser lo hace cumplir.
    if a.estado and (a.aplicar or a.deshacer):
        print("--estado es de solo lectura: no se combina con --aplicar ni --deshacer.",
              file=sys.stderr)
        return 2
    if not a.dsn:
        print("falta DATABASE_URL (o --dsn)", file=sys.stderr)
        return 2

    por_ids = bool(a.ids or a.ids_de_archivo or a.ids_de_env)
    if por_ids and a.condicion:
        print("elegí una cosa: o la lista de ids, o la condicion.", file=sys.stderr)
        return 2

    if a.estado:
        with psycopg.connect(a.dsn, connect_timeout=8) as conn, conn.cursor() as cur:
            r = estado(cur)
        print(f"  PENDIENTES ....... {r['pendientes']:>5} filas  "
              f"({r['pendientes_sesiones']} sesiones)   <- lo que el worker todavia debe")
        print(f"  ya rescoreadas ... {r['servidas']:>5} filas  "
              f"({r['servidas_sesiones']} sesiones)")
        print(f"  ultimo pedido .... {r['ultimo_pedido']}")
        print(f"  ultima servida ... {r['ultima_servida']}")
        if r["pendientes"]:
            print("\nCorrelo de nuevo en un rato: si PENDIENTES no baja, la cola no esta "
                  "avanzando y eso es un bug, no una espera.")
        return 0

    params, ids = None, []
    if a.deshacer:
        # Solo lo NO servido: una fila ya rescoreada no tiene nada que deshacer, y borrarle
        # el pedido perderia el registro de que se pidio.
        cond = "rescore_pedido_at IS NOT NULL AND rescore_pedido_at > scored_at"
    elif por_ids:
        ids, basura = parse_uuids(_leer_ids(a))
        if basura:
            print(f"NO son uuid y se ignoran ({len(basura)}): {basura[:10]}", file=sys.stderr)
        cond, params = condicion_por_ids(ids)
        if cond is None:
            print("no llego ni un uuid valido.", file=sys.stderr)
            return 2
    else:
        cond = (a.condicion or "").strip().rstrip(";")
        if cond.lower() in _PROHIBIDAS:
            print("condicion vacia o universal: eso encola la tabla entera. "
                  "Si es lo que querés, truncá a mano y asumilo.", file=sys.stderr)
            return 2

    with psycopg.connect(a.dsn, connect_timeout=8) as conn, conn.cursor() as cur:
        if ids:
            r = clasificar(cur, ids)
            print(f"llegaron {len(ids)} uuid:")
            for nombre in ("interaccion", "sesion", "ticket"):
                if r[nombre]:
                    print(f"  {len(r[nombre]):>4} como {nombre}")
            if r["sin_match"]:
                # Lo mas importante de todo el reporte: si no se ve, se encola de menos.
                print(f"  {len(r['sin_match']):>4} NO existen en conversation_scores:")
                for i in r["sin_match"][:10]:
                    print(f"         {i}")
        try:
            filas, sesiones = _contar(cur, cond, params)
        except Exception as e:  # noqa: BLE001 - la condicion la escribe una persona
            print(f"la condicion no corre: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        if not ids:
            print(f"condicion: {cond}")
        print(f"  filas (interacciones) ... {filas}")
        print(f"  SESIONES que se rescorean {sesiones}   <- es lo que se paga: el worker "
              f"scorea por sesion")
        if not a.deshacer and filas:
            print("  muestra:")
            for f in _muestra(cur, cond, params):
                print(f"    {f}")
        if not filas:
            print("nada que hacer.")
            return 0
        if not a.aplicar:
            print("\nNO se escribio nada. Volvé a correrlo con --aplicar si el numero cierra.")
            return 0

        valor = "NULL" if a.deshacer else "now()"
        cur.execute(f"UPDATE conversation_scores SET rescore_pedido_at = {valor} "  # noqa: S608
                    f"WHERE {cond}", params)
        conn.commit()
        print(f"\nlisto: {cur.rowcount} filas "
              f"{'desmarcadas' if a.deshacer else 'encoladas'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
