"""Capa de lectura para el dashboard (account-scoped).

REGLA: datos y sistemas conviven en la MISMA base. Toda lectura de scores exige
`account` en el WHERE, para que el dashboard traiga una cuenta u otra segun lo
seleccionado. El transcript se pide aparte (on-demand) porque es pesado.
"""
from __future__ import annotations

from decimal import Decimal

from src.context import fetch_messages

# Filas para las tarjetas/tablas del dashboard: SIN dimensions ni transcript
# (esos van en el detalle). Se unen contacts para el nombre del cliente.
_SCORES_SQL = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment, cs.queue_name,
       cs.user_id, COALESCE(u.name, cs.user_name) AS user_name,
       cs.conversation_created_at, cs.resolved_at,
       cs.rubric, cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       cs.rating_rationale, cs.message_count, cs.agent_message_count,
       cs.bot_message_count, cs.contact_message_count, cs.first_response_seconds,
       cs.resolution_seconds, cs.was_unassigned,
       t.contact_id AS contact_id,
       ct.name AS customer_name, ct.number AS customer_number, t.channel
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE cs.account = %(account)s
 ORDER BY cs.conversation_created_at DESC
"""

_DETAIL_SQL = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment, cs.queue_name,
       cs.user_id, COALESCE(u.name, cs.user_name) AS user_name,
       cs.conversation_created_at, cs.resolved_at,
       cs.rubric, cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       cs.rating_rationale, cs.dimensions, cs.message_count, cs.agent_message_count,
       cs.bot_message_count, cs.contact_message_count, cs.first_response_seconds,
       cs.resolution_seconds, cs.was_unassigned, cs.scoring_version, cs.llm_model,
       ct.name AS customer_name, ct.number AS customer_number, t.channel
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE cs.conversation_id = %(cid)s
"""


def _coerce(v):
    """Postgres `numeric` -> `Decimal` en psycopg. FastAPI/pydantic lo serializa
    como STRING JSON, y el front termina concatenando dígitos en vez de sumar
    (bug del `7.19e+46` en los promedios). Devolvemos float para garantizar un
    número JSON, sin importar el serializador."""
    return float(v) if isinstance(v, Decimal) else v


def _rows_as_dicts(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [{c: _coerce(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def list_accounts(cur) -> list[str]:
    """Cuentas presentes en la tabla de scores (para el selector)."""
    cur.execute(
        "SELECT account, count(*) FROM conversation_scores "
        "WHERE account IS NOT NULL GROUP BY account ORDER BY account"
    )
    return [{"account": a, "count": n} for a, n in cur.fetchall()]


def scored_rows(cur, account: str) -> list[dict]:
    """Todas las conversaciones scoreadas de UNA cuenta (sin transcript)."""
    cur.execute(_SCORES_SQL, {"account": account})
    return _rows_as_dicts(cur)


def _transcript(msgs: list[dict]) -> list[dict]:
    out = []
    for m in msgs:
        if m.get("is_note"):
            continue
        role = "CLIENTE" if not m["from_me"] else ("BOT" if m.get("sent_from") == "CHATBOT" else "AGENTE")
        out.append({"role": role, "text": (m.get("body") or "[media]").strip()[:800]})
    return out


def conversation_detail(cur, conversation_id: str) -> dict | None:
    """Una conversacion con su analisis completo + transcript reconstruido."""
    cur.execute(_DETAIL_SQL, {"cid": conversation_id})
    row = cur.fetchone()
    if not row:
        return None
    cols = [d.name for d in cur.description]
    d = {c: _coerce(v) for c, v in zip(cols, row)}
    d["transcript"] = _transcript(fetch_messages(cur, conversation_id))
    return d
