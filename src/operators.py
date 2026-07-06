"""Reconstruccion del nombre del operador desde el cuerpo de sus mensajes.

La tabla `users` del ETL viene vacia, pero cada operador firma sus mensajes con
el prefijo '*Nombre:*'. Mapeamos user_id -> nombre desde ahi (el nombre mas
frecuente entre los mensajes de ese operador).
"""
from __future__ import annotations

import re
from collections import Counter

_NAME_RE = re.compile(r"^\*([^:*]{2,40}):\*")

# Patron equivalente para Postgres (regexp_match sobre el body).
_PG_NAME_RE = r"^\*([^:*]{2,40}):\*"


def operator_name(messages: list[dict], operator_id) -> str | None:
    """Nombre del operador `operator_id` segun el prefijo de sus mensajes."""
    if not operator_id:
        return None
    names: Counter = Counter()
    for m in messages:
        if m.get("user_id") != operator_id or not m.get("from_me") or m.get("is_note"):
            continue
        match = _NAME_RE.match((m.get("body") or "").strip())
        if match:
            names[match.group(1).strip()] += 1
    return names.most_common(1)[0][0] if names else None


def build_operator_map(cur, account: str | None = None) -> dict[str, str]:
    """Mapa GLOBAL user_id -> nombre, leyendo la firma '*Nombre:*' de TODOS los
    mensajes del operador (no solo de una conversacion).

    Resuelve operadores que en una conversacion puntual no firmaron pero si lo
    hicieron en otra. Se scopea por cuenta (un user_id pertenece a una cuenta).
    """
    where_acc = "AND account = %s" if account else ""
    cur.execute(
        f"""SELECT user_id,
                   (regexp_match(body, '{_PG_NAME_RE}'))[1] AS name,
                   count(*) AS n
              FROM messages
             WHERE from_me AND NOT is_note AND user_id IS NOT NULL
               AND body ~ '{_PG_NAME_RE}' {where_acc}
             GROUP BY user_id, name""",
        (account,) if account else None,
    )
    best: dict[str, tuple[str, int]] = {}
    for user_id, name, n in cur.fetchall():
        if not name:
            continue
        key = str(user_id)
        if key not in best or n > best[key][1]:
            best[key] = (name, n)
    return {k: v[0] for k, v in best.items()}
