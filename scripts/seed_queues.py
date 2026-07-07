"""Seed de la tabla `queues` desde los JSON de salida del ETL.

La tabla `queues` viene poblada en prod pero VACIA en el dump local, así que
sin nombres de cola NO hay segmento (todo cae en 'interno') y los cuadros del
análisis (segmento jugador) no se pueden armar.

A diferencia de `users` (reconstruible por la firma en los mensajes), el nombre
de la cola solo vive en los audit del ETL: output/<cuenta>/whaticket_audit-*.json,
donde cada registro trae queueId + queueName. Este script agrega el nombre más
frecuente por (cuenta, queueId) y hace UPSERT en `queues`. Idempotente.

Correr tras cada restore local. El dir de salida del ETL se toma de AUDIT_DIR
(por defecto ../ETLWhaticket/output, hermano del repo).

Uso:
    python -m scripts.seed_queues            # aplica
    python -m scripts.seed_queues --dry-run  # solo reporta
    AUDIT_DIR=/ruta/output python -m scripts.seed_queues
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter
from pathlib import Path

import psycopg

from src.config import load_config

_DEFAULT_AUDIT = Path(__file__).resolve().parent.parent.parent / "ETLWhaticket" / "output"
_UPSERT = """
INSERT INTO queues (id, account, name)
VALUES (%(id)s, %(account)s, %(name)s)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, account = EXCLUDED.account
"""


def collect(audit_dir: Path) -> dict[tuple[str, str], str]:
    """(cuenta, queueId) -> nombre más frecuente, leído de los audit del ETL."""
    names: dict[tuple[str, str], Counter] = {}
    for f in glob.glob(str(audit_dir / "*" / "whaticket_audit-*.json")):
        account = Path(f).parent.name
        try:
            recs = json.load(open(f, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in recs if isinstance(recs, list) else []:
            qid, qn = r.get("queueId"), r.get("queueName")
            if qid and qn:
                names.setdefault((account, qid), Counter())[qn] += 1
    return {k: c.most_common(1)[0][0] for k, c in names.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    args = ap.parse_args()

    audit_dir = Path(os.environ.get("AUDIT_DIR", _DEFAULT_AUDIT))
    mapping = collect(audit_dir)
    if not mapping:
        print(f"[seed_queues] sin audit en {audit_dir} — nada para sembrar")
        return

    cfg = load_config()
    if not args.dry_run:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                for (account, qid), name in mapping.items():
                    cur.execute(_UPSERT, {"id": qid, "account": account, "name": name})
            conn.commit()

    verbo = "sembraría" if args.dry_run else "sembró"
    print(f"[seed_queues] {verbo} {len(mapping)} colas desde {audit_dir}")
    for (account, qid), name in sorted(mapping.items()):
        print(f"  {account:9} {qid} -> {name}")


if __name__ == "__main__":
    main()
