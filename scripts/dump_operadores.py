#!/usr/bin/env python3
"""BD -> config/operadores.json. El archivo es el ESPEJO auditable del estado en la base.

POR QUE EXISTE. Al server solo se le puede hacer `git pull`; no podemos leer sus archivos.
Si alguien apaga un operador desde el modal del dashboard en produccion, ese cambio queda
en la tabla `operator_status` y es invisible desde afuera. Este script se corre sobre la
COPIA de prod y escribe el archivo: el diff de git muestra exactamente que cambiaron.

    # espejo del estado actual de la base (lo normal)
    python scripts/dump_operadores.py

    # bootstrap: derivar el estado desde la ACTIVIDAD, sin leer la tabla
    python scripts/dump_operadores.py --desde-actividad --umbral 100 --dias 30

    # ver el resultado sin escribir nada
    python scripts/dump_operadores.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.config import load_config  # noqa: E402
from src.operators_status import (  # noqa: E402
    CONFIG_PATH,
    activity_rows,
    config_from_rows,
    config_rows,
    dump_config,
    ensure_table,
    load_config as load_operadores_config,
    suggest_from_activity,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--desde-actividad", action="store_true",
                    help="derivar activo/inactivo de la actividad en vez de leer la tabla")
    ap.add_argument("--umbral", type=int, default=100,
                    help="sesiones recientes minimas para considerar ACTIVO (default 100)")
    ap.add_argument("--dias", type=int, default=30,
                    help="ventana de actividad reciente, en dias (default 30)")
    ap.add_argument("--salida", default=str(CONFIG_PATH), help="ruta del archivo a escribir")
    ap.add_argument("--dry-run", action="store_true", help="imprimir sin escribir")
    args = ap.parse_args()

    cfg = load_config()
    ahora = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            if args.desde_actividad:
                filas = suggest_from_activity(activity_rows(cur, args.dias), args.umbral)
                criterio = f"sesiones en {args.dias} dias >= {args.umbral}"
                nueva = config_from_rows(filas, criterio=criterio, generado_en=ahora)
            else:
                nueva = dump_config(cur, criterio="estado de la tabla operator_status",
                                    generado_en=ahora)
        conn.commit()  # ensure_table pudo crear la tabla

    # El archivo lista SOLO apagados: los activos son el default y no se escriben.
    filas_nuevas = config_rows(nueva)
    print(f"{len(filas_nuevas)} operadores APAGADOS (el resto queda activo por default)")
    for cuenta, ops in sorted(nueva["apagados"].items()):
        print(f"  {cuenta}: {len(ops)} apagados")

    salida = Path(args.salida)
    # DIFF contra lo que ya estaba: es el punto del script (ver qué cambiaron en el server).
    # Si el archivo previo no se puede leer (editado a mano con un error, o de un formato
    # viejo) se avisa y se sigue: el dump tiene que poder regenerar igual, no quedar
    # bloqueado por el estado anterior.
    previo = None
    if salida.exists():
        try:
            previo = load_operadores_config(salida)
        except ValueError as exc:
            print(f"  (no se pudo leer el archivo previo para el diff: {exc})")
    if previo is not None:
        antes = {(c, o) for c, o, _ in config_rows(previo)}
        despues = {(c, o) for c, o, _ in filas_nuevas}
        for k in sorted(antes ^ despues):
            estado = "APAGADO" if k in despues else "reactivado"
            print(f"  CAMBIO {k[0]}/{k[1]}: {estado}")
        if not (antes ^ despues):
            print("  sin cambios respecto del archivo actual")

    if args.dry_run:
        print("\n--dry-run: no se escribio nada")
        return 0
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(nueva, ensure_ascii=False, indent=2) + "\n")
    print(f"\nescrito: {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
