"""Aplica db/scores_schema.sql a la BD: crea la tabla `conversation_scores`.

El arranque del app NO crea esta tabla: ensure_indexes() solo asegura los
indices de `messages` (tablas del ETL). En un deploy nuevo la BD de prod solo
tiene las tablas del ETL, asi que /api/scores y /api/accounts responden 500
("relation conversation_scores does not exist") y el front corta con
"No hay datos scoreados todavia" (ni siquiera pinta los cuadros del ETL).

Correr UNA vez tras el primer deploy, ANTES de encender el worker
(SCORING_ENABLED=true): el worker tambien hace INSERT contra esta tabla.

Idempotente: el schema usa CREATE TABLE/INDEX IF NOT EXISTS; re-correrlo es
inofensivo.

Uso (dentro del contenedor del dashboard, que ya trae psycopg y el .sql):
    python -m scripts.apply_scores_schema
"""
from __future__ import annotations

from pathlib import Path

import psycopg

from src.config import load_config

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "scores_schema.sql"


def main() -> None:
    cfg = load_config()
    sql = _SCHEMA.read_text(encoding="utf-8")
    # autocommit=True: el script ya trae su propio BEGIN;/COMMIT;, asi que la
    # transaccion la maneja el propio SQL, no psycopg.
    with psycopg.connect(cfg.database_url, autocommit=True) as conn:
        # pgconn.exec_() usa el protocolo SIMPLE de libpq -> admite multiples
        # statements en un solo envio (Cursor.execute usa el protocolo extendido,
        # que rechaza scripts multi-statement).
        res = conn.pgconn.exec_(sql.encode("utf-8"))
        err = res.error_message.decode("utf-8", "replace").strip()
        if err:
            raise RuntimeError(f"scores_schema fallo: {err}")
    print("scores_schema aplicado OK: tabla conversation_scores lista.")


if __name__ == "__main__":
    main()
