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
#
# PAYLOAD: esta lista trae TODA la cuenta (el front filtra en memoria). En sistemas
# son ~113k filas -> el rating_rationale completo (parrafo del LLM) pesaba el 40% del
# JSON (~112MB/13s). En la lista solo se usa como snippet -> se trunca a 160 chars; el
# texto completo lo sirve _DETAIL_SQL al abrir el modal. Los campos que SOLO consume
# ese modal (metaGrid: *_seconds, *_message_count, was_unassigned, rubric) y los no
# usados (queue_name, resolved_at) se omiten aca: peso muerto en la lista.
_SCORES_SELECT = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment,
       cs.user_id, COALESCE(u.name, cs.user_name) AS user_name,
       cs.conversation_created_at,
       cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       left(cs.rating_rationale, 160) AS rating_rationale, cs.deposit_count,
       t.contact_id AS contact_id,
       ct.name AS customer_name, ct.number AS customer_number, t.channel
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE cs.account = %(account)s"""

# B1: ventana móvil opcional para la carga inicial. sistemas tiene 113k scoreadas
# -> traerlas todas es ~112MB/13s. Con ventana, el front pide solo los últimos N
# meses (con toggle a histórico completo). Anclado al mes MÁS RECIENTE de la cuenta
# (no now(): el dataset puede estar pausado), igual que los cuadros (_MONTH_WINDOW).
_SCORES_WINDOW = """
   AND cs.conversation_created_at >= (SELECT date_trunc('month', max(conversation_created_at))
                                        FROM conversation_scores WHERE account = %(account)s)
                                      - make_interval(months => %(months_back)s)"""

_SCORES_ORDER = "\n ORDER BY cs.conversation_created_at DESC"

_DETAIL_SQL = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment, cs.queue_name,
       cs.user_id, COALESCE(u.name, cs.user_name) AS user_name,
       cs.conversation_created_at, cs.resolved_at,
       cs.rubric, cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       cs.rating_rationale, cs.deposit_count, cs.dimensions, cs.message_count, cs.agent_message_count,
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


def scored_rows(cur, account: str, window_months: int | None = None) -> list[dict]:
    """Conversaciones scoreadas de UNA cuenta (sin transcript).

    window_months=None -> histórico completo (backward-compatible).
    window_months=N    -> solo los últimos N meses (mes más reciente + N-1 previos),
                          para aligerar la carga inicial del dashboard."""
    params = {"account": account}
    sql = _SCORES_SELECT
    if window_months:
        sql += _SCORES_WINDOW
        params["months_back"] = window_months - 1
    cur.execute(sql + _SCORES_ORDER, params)
    return _rows_as_dicts(cur)


def _transcript(msgs: list[dict]) -> list[dict]:
    out = []
    for m in msgs:
        if m.get("is_note"):
            continue
        role = "CLIENTE" if not m["from_me"] else ("BOT" if m.get("sent_from") == "CHATBOT" else "AGENTE")
        out.append({"role": role, "text": (m.get("body") or "[media]").strip()[:800]})
    return out


DEFAULT_WINDOW_MONTHS = 12

# Ventana móvil: solo los últimos N meses, anclada al MES MÁS RECIENTE de la cuenta
# (no a now(): el dataset puede quedar pausado/histórico). Mantiene los cuadros
# legibles y el top-N reflejando a los operadores actuales, no a los de hace años.
# %(months_back)s = N-1 (el mes más reciente + los N-1 previos = N meses).
_MONTH_WINDOW = """
   AND c.created_at >= (SELECT date_trunc('month', max(created_at))
                          FROM conversations WHERE account = %(account)s)
                        - make_interval(months => %(months_back)s)"""


# --- §10: carga mensual por operador (segmento jugador). Operador = el user_id
# con más mensajes de negocio en la conversación (conversations.user_id suele ser
# NULL). Se acota a las colas jugador y se agrupa por (mes, operador).
_LOAD_SQL = """
WITH msg_op AS (
  SELECT conversation_id, user_id, count(*) AS n
    FROM messages
   WHERE account = %(account)s AND from_me AND NOT is_note AND user_id IS NOT NULL
   GROUP BY conversation_id, user_id
),
conv_op AS (
  SELECT DISTINCT ON (conversation_id) conversation_id, user_id
    FROM msg_op ORDER BY conversation_id, n DESC
)
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       coalesce(u.name, 'Sin identificar') AS op,
       count(*) AS conv
  FROM conversations c
  JOIN conv_op co ON co.conversation_id = c.id
  LEFT JOIN users u ON u.id = co.user_id
 WHERE c.account = %(account)s AND c.created_at IS NOT NULL AND c.queue_id = ANY(%(qids)s)""" + _MONTH_WINDOW + """
 GROUP BY 1, 2
"""


def _jugador_queue_ids(cur, account: str) -> list:
    """IDs de las colas del segmento jugador (clasificadas con segment_for_queue)."""
    from src.segments import segment_for_queue

    cur.execute("SELECT id, name FROM queues WHERE account = %s", (account,))
    return [qid for qid, name in cur.fetchall() if segment_for_queue(name) == "jugador"]


def _build_load_series(rows, top_n: int) -> dict:
    """Arma {months, series[]} desde filas (mes, op, conv): top-N operadores por
    volumen + 'Otros' (el resto sumado). Lógica pura, testeable sin DB."""
    months = sorted({r[0] for r in rows})
    by_op: dict[str, dict[str, int]] = {}
    for mes, op, conv in rows:
        by_op.setdefault(op, {})[mes] = int(conv)
    totals = {op: sum(m.values()) for op, m in by_op.items()}
    ranked = sorted(totals, key=lambda o: (-totals[o], o))
    top, rest = ranked[:top_n], ranked[top_n:]
    series = [{"op": op, "data": [by_op[op].get(m, 0) for m in months]} for op in top]
    if rest:
        series.append({"op": "Otros", "data": [sum(by_op[o].get(m, 0) for o in rest) for m in months]})
    return {"months": months, "series": series}


def load_by_operator(cur, account: str, top_n: int = 7,
                     window_months: int = DEFAULT_WINDOW_MONTHS) -> dict:
    """Carga mensual por operador (jugadores), top-N + 'Otros', últimos N meses."""
    qids = _jugador_queue_ids(cur, account)
    if not qids:
        return {"months": [], "series": []}
    cur.execute(_LOAD_SQL, {"account": account, "qids": qids, "months_back": window_months - 1})
    return _build_load_series(cur.fetchall(), top_n)


# --- §2: % depósito en WhatsApp por operador (jugador). Une operador dominante +
# flag de depósito por conversación, acotado a WhatsApp y colas jugador.
_DEP_PCT_SQL = """
WITH msg_op AS (
  SELECT conversation_id, user_id, count(*) AS n
    FROM messages
   WHERE account = %(account)s AND from_me AND NOT is_note AND user_id IS NOT NULL
   GROUP BY conversation_id, user_id
),
conv_op AS (
  SELECT DISTINCT ON (conversation_id) conversation_id, user_id
    FROM msg_op ORDER BY conversation_id, n DESC
),
conv_dep AS MATERIALIZED (
  SELECT conversation_id,
         bool_or((body ~* %(re)s) AND NOT is_note) AS has_ctx,
         count(*) FILTER (WHERE from_me = false AND NOT is_note
                          AND lower(coalesce(media_type, '')) LIKE '%%image%%') AS img
    FROM messages WHERE account = %(account)s GROUP BY conversation_id
)
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       coalesce(u.name, 'Sin identificar') AS op,
       count(*) AS conv,
       count(*) FILTER (WHERE cd.has_ctx AND cd.img > 0) AS con_dep
  FROM conversations c
  JOIN conv_op co ON co.conversation_id = c.id
  LEFT JOIN conv_dep cd ON cd.conversation_id = c.id
  LEFT JOIN users u ON u.id = co.user_id
  JOIN tickets t ON t.id = c.ticket_id
 WHERE c.account = %(account)s AND c.queue_id = ANY(%(qids)s)
   AND t.channel = 'WHATSAPP' AND c.created_at IS NOT NULL""" + _MONTH_WINDOW + """
 GROUP BY 1, 2
"""


def _build_pct_series(rows, top_n: int, min_conv: int = 8) -> dict:
    """{months, series[]} de % depósito desde filas (mes, op, conv, con_dep):
    top-N por volumen + 'Otros'. Mes-operador con <min_conv conv -> None (se omite
    del gráfico, como en el PDF; evita % ruidoso de bajo volumen). Puro/testeable."""
    months = sorted({r[0] for r in rows})
    by_op: dict[str, dict[str, tuple[int, int]]] = {}
    for mes, op, conv, con_dep in rows:
        by_op.setdefault(op, {})[mes] = (int(conv), int(con_dep))
    totals = {op: sum(c for c, _ in m.values()) for op, m in by_op.items()}
    ranked = sorted(totals, key=lambda o: (-totals[o], o))
    top, rest = ranked[:top_n], ranked[top_n:]

    def pct(conv, dep):
        return round(100.0 * dep / conv, 1) if conv >= min_conv else None

    series = []
    for op in top:
        series.append({"op": op, "data": [pct(*by_op[op][m]) if m in by_op[op] else None for m in months]})
    if rest:
        data = []
        for m in months:
            c = sum(by_op[o].get(m, (0, 0))[0] for o in rest)
            d = sum(by_op[o].get(m, (0, 0))[1] for o in rest)
            data.append(pct(c, d))
        series.append({"op": "Otros", "data": data})
    return {"months": months, "series": series}


def deposit_pct_by_operator(cur, account: str, top_n: int = 7, min_conv: int = 8,
                            window_months: int = DEFAULT_WINDOW_MONTHS) -> dict:
    """§2: % depósito en WhatsApp por operador (jugadores), top-N + 'Otros', últimos N meses."""
    from src.deposits import RECHARGE_PATTERN

    qids = _jugador_queue_ids(cur, account)
    if not qids:
        return {"months": [], "series": []}
    cur.execute(_DEP_PCT_SQL, {"account": account, "re": RECHARGE_PATTERN, "qids": qids,
                               "months_back": window_months - 1})
    return _build_pct_series(cur.fetchall(), top_n, min_conv)


# --- §9: nuevos jugadores vs % depósito por mes (jugador, agregado). Dos medidas
# de escala distinta -> el front las muestra en DOS paneles (no doble-eje).
_NEW_VS_DEP_SQL = """
WITH per_conv AS MATERIALIZED (
  SELECT conversation_id,
         bool_or((body ~* %(re)s) AND NOT is_note) AS has_ctx,
         count(*) FILTER (WHERE from_me = false AND NOT is_note
                          AND lower(coalesce(media_type, '')) LIKE '%%image%%') AS img
    FROM messages WHERE account = %(account)s GROUP BY conversation_id
)
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       count(*) AS conv,
       count(*) FILTER (WHERE pc.has_ctx AND pc.img > 0) AS con_dep,
       count(*) FILTER (WHERE c.is_new_contact) AS nuevos
  FROM conversations c
  LEFT JOIN per_conv pc ON pc.conversation_id = c.id
 WHERE c.account = %(account)s AND c.queue_id = ANY(%(qids)s) AND c.created_at IS NOT NULL""" + _MONTH_WINDOW + """
 GROUP BY 1
"""


def _build_new_vs_deposit(rows) -> dict:
    """{months, nuevos[], pct[]} desde filas (mes, conv, con_dep, nuevos). Puro."""
    rows = sorted(rows, key=lambda r: r[0])
    months = [r[0] for r in rows]
    nuevos = [int(r[3]) for r in rows]
    pct = [round(100.0 * int(r[2]) / int(r[1]), 1) if int(r[1]) else 0.0 for r in rows]
    return {"months": months, "nuevos": nuevos, "pct": pct}


def new_vs_deposit_by_month(cur, account: str,
                            window_months: int = DEFAULT_WINDOW_MONTHS) -> dict:
    """§9: nuevos jugadores y % depósito por mes (segmento jugador), últimos N meses."""
    from src.deposits import RECHARGE_PATTERN

    qids = _jugador_queue_ids(cur, account)
    if not qids:
        return {"months": [], "nuevos": [], "pct": []}
    cur.execute(_NEW_VS_DEP_SQL, {"account": account, "re": RECHARGE_PATTERN, "qids": qids,
                                  "months_back": window_months - 1})
    return _build_new_vs_deposit(cur.fetchall())


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
