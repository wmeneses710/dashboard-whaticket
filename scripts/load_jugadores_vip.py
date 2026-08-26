#!/usr/bin/env python3
"""config/jugadores_vip.json -> BD. Empuja la marca VIP a la tabla `vip_players`.

ES EL PASO QUE SACA EL DATO PERSONAL DEL REPO. El archivo lleva la REFERENCIA
(`contact_id`, un uuid) y la marca queda en la base, que ya tiene esos telefonos en
`contacts`. Ver src/vip.py para por que va en tabla propia y no en una columna de
`contacts`.

DOS MODOS, la misma distincion que scripts/load_operadores.py y por el mismo motivo:

  --seed   (default)  ON CONFLICT DO NOTHING: solo LLENA huecos. Respeta cualquier VIP
                      que alguien haya apagado a mano en produccion, que es justo lo que
                      no podemos ver desde afuera.
  --pisar             UPSERT: el archivo GANA. Para restaurar un estado conocido o para
                      empujar un reporte nuevo ya revisado.

    python scripts/load_jugadores_vip.py                # seed, no pisa nada
    python scripts/load_jugadores_vip.py --pisar        # el archivo manda
    python scripts/load_jugadores_vip.py --dry-run      # mostrar el efecto y salir

LOS `baja` ENTRAN APAGADOS. Son menciones del username en varios contactos --`quezada`
cae en 20 y `medardo` en 10, porque son apellidos--. La referencia se guarda para que se
vea que fueron evaluados; encenderlos es una decision del negocio, y se hace en la tabla.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.vip import (  # noqa: E402
    apply_config, base_de, filas_de_config, filas_huerfanas, podar, verificar_origen)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "jugadores_vip.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--pisar", action="store_true", help="el archivo gana (UPSERT)")
    ap.add_argument("--podar", action="store_true",
                    help="BORRAR las filas que el archivo ya no tiene (destructivo)")
    ap.add_argument("--dry-run", action="store_true", help="mostrar el efecto y salir")
    args = ap.parse_args()

    doc = json.loads(Path(args.config).read_text(encoding="utf-8"))
    jugadores = doc["jugadores"]
    filas = filas_de_config(jugadores)
    enc = sum(1 for f in filas if f["es_vip"])
    por_cuenta = collections.Counter(f["account"] for f in filas)
    conf = collections.Counter(f["confianza"] for f in filas)
    print(f"  origen    : {doc.get('origen_bd') or 'sin estampar'}")
    print(f"{args.config}: {len(jugadores)} jugadores -> {len(filas)} filas "
          f"({enc} encendidas, {len(filas) - enc} apagadas)")
    print(f"  por cuenta: {dict(por_cuenta)}")
    print(f"  confianza : {dict(conf)}")
    if args.dry_run:
        print("dry-run: no se escribio nada")
        return 0

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("falta DATABASE_URL", file=sys.stderr)
        return 2
    # EL GUARD MAS IMPORTANTE DEL DESPLIEGUE: que los `contact_id` sean de ESTA base.
    aviso = verificar_origen(doc, base_de(dsn))
    if aviso:
        print(f"OJO: {aviso}", file=sys.stderr)
    with psycopg.connect(dsn) as cn, cn.cursor() as cur:
        n = apply_config(cur, jugadores, pisar=args.pisar)
        # LAS HUERFANAS SE AVISAN SIEMPRE. Un vinculo que dejo de ser valido no se corrige
        # con un upsert: la fila vieja se queda alertando. Asi quedaron tres GRUPOS de
        # WhatsApp adentro despues de arreglar el dump.
        huerfanas = filas_huerfanas(cur, jugadores)
        borradas = podar(cur, jugadores) if args.podar else 0
        cn.commit()
    print(f"{'pisado' if args.pisar else 'seed'}: {n} filas enviadas a vip_players")
    if huerfanas:
        if args.podar:
            print(f"podadas: {borradas} filas que el archivo ya no tiene")
        else:
            print(f"OJO: {len(huerfanas)} filas en la tabla que el archivo ya NO tiene. "
                  f"Siguen alertando. Correr con --podar para borrarlas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
