"""Inspecciona el scoring POR FLUJO (como lo muestra el dashboard), no por fila.

El front agrupa los scores por PERSONA (contact_id) y muestra un promedio de las
estrellas de sus visitas evaluadas (web/index.html, "una tarjeta = una PERSONA").
Este script reconstruye esa vista en texto: por cada flujo (contacto) imprime sus
visitas en orden, con el score de cada una y un snippet del transcript que vio el
modelo. Sirve para VER cómo el grano por-visita se agrega en la tarjeta.

Uso (dentro del contenedor, con acceso a la BD):
    python -m scripts.inspect_flow                 # cuenta 'datos', flujos multi-visita
    python -m scripts.inspect_flow sistemas 8      # otra cuenta, 8 flujos
"""
from __future__ import annotations

import sys
from collections import defaultdict

import psycopg

from src.config import load_config
from src.context import fetch_messages
from src.queries import _transcript

_ROWS_SQL = """
SELECT cs.conversation_id, cs.ticket_id, t.contact_id,
       COALESCE(ct.name, '(sin nombre)') AS name,
       cs.conversation_created_at, cs.eval_status, cs.skip_reason,
       cs.rating_label, cs.stars, cs.deposit_count
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
 WHERE cs.account = %(account)s
 ORDER BY cs.conversation_created_at
"""


def _flow_key(row: dict) -> str:
    # Mismo criterio que el front: contact_id si existe; si no, ticket/conversation.
    if row["contact_id"] is not None:
        return f"c{row['contact_id']}"
    return f"t{row['ticket_id'] or row['conversation_id']}"


def _snippet(cur, conversation_id) -> str:
    msgs = _transcript(fetch_messages(cur, conversation_id))
    parts = [f"{m['role'][:4]}:{m['text'][:50]}" for m in msgs[:4]]
    return "  |  ".join(parts) if parts else "(sin mensajes de negocio)"


def main() -> None:
    account = sys.argv[1] if len(sys.argv) > 1 else "datos"
    n_flows = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    cfg = load_config()
    with psycopg.connect(cfg.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_ROWS_SQL, {"account": account})
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        flows: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            flows[_flow_key(r)].append(r)

        # Flujos multi-visita (los que exponen el problema del grano), con
        # peor promedio primero para ver los casos dudosos.
        def card_avg(visits):
            ev = [float(v["stars"]) for v in visits if v["eval_status"] == "evaluated" and v["stars"] is not None]
            return sum(ev) / len(ev) if ev else None

        multi = [(k, v) for k, v in flows.items() if len(v) > 1]
        multi.sort(key=lambda kv: (card_avg(kv[1]) is None, card_avg(kv[1]) or 0))

        print(f"== cuenta={account} · {len(rows)} visitas scoreadas · "
              f"{len(flows)} flujos ({len(multi)} multi-visita) ==\n")

        for key, visits in multi[:n_flows]:
            avg = card_avg(visits)
            avg_txt = f"{avg:.1f}" if avg is not None else "s/e"
            name = visits[0]["name"]
            print(f"### FLUJO {name}  ·  {len(visits)} visitas  ·  tarjeta prom={avg_txt}★")
            with conn.cursor() as cur:
                for v in visits:
                    if v["eval_status"] == "evaluated":
                        tag = f"{v['stars']}★ {v['rating_label']}"
                    else:
                        tag = f"SKIP ({v['skip_reason']})"
                    dep = " [dep]" if (v["deposit_count"] or 0) > 0 else ""
                    print(f"  - {v['conversation_created_at']:%m-%d %H:%M} "
                          f"[{tag}]{dep}  {_snippet(cur, v['conversation_id'])}")
            print()


if __name__ == "__main__":
    main()
