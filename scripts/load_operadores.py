#!/usr/bin/env python3
"""config/operadores.json -> BD. Empuja el archivo a la tabla `operator_status`.

DOS MODOS, y la diferencia importa:

  --seed   (default)  ON CONFLICT DO NOTHING: solo LLENA huecos. Es lo mismo que corre al
                      arrancar el contenedor. Respeta cualquier cambio hecho desde el modal
                      en produccion, que es justo lo que no podemos ver desde afuera.
  --pisar             UPSERT: el archivo GANA. Para restaurar un estado conocido despues de
                      una copia, o para empujar un cambio ya revisado en git.

    python scripts/load_operadores.py                 # seed, no pisa nada
    python scripts/load_operadores.py --pisar         # el archivo manda
    python scripts/load_operadores.py --dry-run       # mostrar el efecto y salir
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.config import load_config  # noqa: E402
from src.operators_status import (  # noqa: E402
    CONFIG_PATH,
    apply_config,
    config_rows,
    ensure_table,
    inactive_names,
    load_config as load_operadores_config,
    seed_from_config,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archivo", default=str(CONFIG_PATH))
    ap.add_argument("--pisar", action="store_true",
                    help="UPSERT: el archivo gana sobre lo que haya en la BD")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"no existe {ruta} — genéralo con scripts/dump_operadores.py")
        return 1
    operadores = load_operadores_config(ruta)   # valida; falla fuerte si está mal
    filas = config_rows(operadores)
    modo = "PISAR (apaga los listados y REACTIVA al resto)" if args.pisar else "SEED (no pisa)"
    print(f"{ruta}: {len(filas)} apagados · modo {modo}")

    cfg = load_config()
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            # Estado ANTES, para poder reportar el efecto real en vez de "listo".
            antes = {c: inactive_names(cur, c) for c in {f[0] for f in filas}}
            if args.dry_run:
                conn.rollback()
                for cuenta, apagados in sorted(antes.items()):
                    quiere = {o for c, o, _ in filas if c == cuenta}
                    # PISAR: el archivo manda tal cual. SEED: solo agrega, nunca reactiva.
                    fin = quiere if args.pisar else (apagados | quiere)
                    print(f"  {cuenta}: apagados hoy {len(apagados)} -> {len(fin)}")
                    if args.pisar and (apagados - quiere):
                        print(f"    se REACTIVARIAN: {', '.join(sorted(apagados - quiere))}")
                print("--dry-run: no se escribio nada")
                return 0
            n = apply_config(cur, operadores) if args.pisar else seed_from_config(cur, operadores)
            despues = {c: inactive_names(cur, c) for c in {f[0] for f in filas}}
        conn.commit()

    print(f"{n} filas enviadas")
    for cuenta in sorted(despues):
        print(f"  {cuenta}: apagados {len(antes.get(cuenta, set()))} -> {len(despues[cuenta])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
