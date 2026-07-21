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
_SCORES_SQL = """
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
 WHERE cs.account = %(account)s
 ORDER BY cs.conversation_created_at DESC
"""

_DETAIL_SQL = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment, cs.queue_name,
       cs.user_id, COALESCE(u.name, cs.user_name) AS user_name,
       cs.conversation_created_at, cs.resolved_at,
       cs.rubric, cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       cs.rating_rationale, cs.deposit_count, cs.dimensions, cs.message_count, cs.agent_message_count,
       cs.bot_message_count, cs.contact_message_count, cs.first_response_seconds,
       cs.resolution_seconds, cs.was_unassigned, cs.scoring_version, cs.llm_model,
       cs.rating_applicable, cs.atencion, cs.deposit_observed, cs.motivo,
       ct.name AS customer_name, ct.number AS customer_number, t.channel,
       pc.returned AS conversion_returned,
       EXTRACT(EPOCH FROM (ses.end_at - ses.start_at)) AS session_seconds
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
  -- pc.returned no-NULL solo si ESTA conversacion es la de ENTRADA de una persona
  -- (first_conversation_id). Sirve para el label "convirtio a jugador" en el chat.
  LEFT JOIN player_conversions pc ON pc.first_conversation_id = cs.conversation_id
  -- duración de la sesión (end_at - start_at, ambos = tiempos de mensaje reales tras el
  -- fix de freshness) para el flag de CIERRE RÁPIDO en el chat.
  LEFT JOIN conversation_sessions ses ON ses.session_id = cs.session_id
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


# --- B2: agregados server-side. En vez de mandar las ~113k filas al cliente para
# que agregue en memoria (~112MB/13s), la BD calcula los KPIs/distribución/ops y
# devuelve unos KB. Los filtros del front (matchBase) se traducen a un WHERE común.
# 'rating' bucketea por estrella (excelente=5 ... mala=1), igual que bucketOf.
_RATING_STARS = {"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1}


def _scores_filters(account: str, *, estado="all", segment="all", canal="all",
                    op="all", date_from=None, date_to=None, rating="all",
                    search="", motivo="all") -> tuple[str, dict]:
    """(where_sql, params) para conversation_scores, replicando matchBase del front.
    Los valores van SIEMPRE como parámetros (%(...)s); el SQL solo arma columnas."""
    where = ["cs.account = %(account)s"]
    params: dict = {"account": account}
    if estado and estado != "all":
        where.append("cs.eval_status = %(estado)s"); params["estado"] = estado
    if motivo and motivo != "all":
        where.append("cs.motivo = %(motivo)s"); params["motivo"] = motivo
    if segment and segment != "all":
        where.append("cs.segment = %(segment)s"); params["segment"] = segment
    if canal and canal != "all":
        where.append("t.channel = %(canal)s"); params["canal"] = canal
    if op and op != "all":
        where.append("COALESCE(u.name, cs.user_name) = %(op)s"); params["op"] = op
    if date_from:
        where.append("cs.conversation_created_at >= %(dfrom)s"); params["dfrom"] = date_from
    if date_to:
        where.append("cs.conversation_created_at <= %(dto)s"); params["dto"] = date_to
    if rating and rating != "all" and rating in _RATING_STARS:
        where.append("cs.stars = %(rstars)s"); params["rstars"] = _RATING_STARS[rating]
    if search:
        where.append("(ct.name ILIKE %(q)s OR ct.number ILIKE %(q)s "
                     "OR COALESCE(u.name, cs.user_name) ILIKE %(q)s)")
        params["q"] = f"%{search}%"
    return " AND ".join(where), params


_SCORES_JOINS = """
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE {where}"""

# KPIs = renderKpis del front: total, evaluadas, ★ promedio (solo evaluadas),
# depósitos (suma), conversaciones con depósito, operadores distintos (evaluadas).
_SUMMARY_KPIS_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated') AS evaluadas,
       avg(cs.stars) FILTER (WHERE cs.eval_status = 'evaluated') AS avg_stars,
       coalesce(sum(cs.deposit_count), 0) AS depositos,
       count(*) FILTER (WHERE cs.deposit_count > 0) AS dep_conv,
       count(DISTINCT coalesce(nullif(coalesce(u.name, cs.user_name), ''), cs.user_id::text))
             FILTER (WHERE cs.eval_status = 'evaluated') AS operadores""" + _SCORES_JOINS


# "Pendiente de evaluar" = sesión CERRADA (end_at < now-6h, misma condición que el
# worker en PENDING_SESSIONS_SQL) que todavía NO tiene score al día. Es la señal de
# "hay backfill en curso": el dashboard escaso no es un agujero, es proceso. Scopeado
# por cuenta + rango de fechas (sobre start_at); los otros filtros (segmento/rating)
# no aplican a lo aún-no-scoreado.
_PENDING_SESSIONS_COUNT_SQL = """
SELECT count(*) AS pendientes
  FROM conversation_sessions cs
 WHERE cs.account = %(account)s
   AND cs.end_at < now() - interval '6 hours'
   AND NOT EXISTS (
     SELECT 1 FROM conversation_scores s
      WHERE s.session_id = cs.session_id AND s.scored_at >= cs.end_at)
   {date_clause}"""


def pending_sessions_count(cur, account: str, date_from=None, date_to=None) -> int:
    """Sesiones cerradas de la cuenta que aún no fueron scoreadas (backfill en curso)."""
    params: dict = {"account": account}
    clause = ""
    if date_from:
        clause += " AND cs.start_at >= %(dfrom)s"; params["dfrom"] = date_from
    if date_to:
        clause += " AND cs.start_at <= %(dto)s"; params["dto"] = date_to
    cur.execute(_PENDING_SESSIONS_COUNT_SQL.format(date_clause=clause), params)
    return int(cur.fetchone()[0])


# "Cierre rápido": sesión EVALUADA que cerró muy rápido (<10min) Y sin resolver (★<=2).
# Señal DIAGNÓSTICA — la conversación concluyó rápido sin solucionar al usuario; puede
# ser deficiencia de configuración (auto-close agresivo), no siempre culpa del agente.
# Un depósito resuelto en 3min NO cae acá (★>2). Medible gracias al fix de end_at.
_FAST_CLOSE_SQL = """
SELECT count(*) AS cierres_rapidos""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated' AND cs.stars <= 2 AND cs.session_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM conversation_sessions ses
                WHERE ses.session_id = cs.session_id
                  AND ses.end_at - ses.start_at < interval '10 minutes')"""


def fast_close_count(cur, account: str, **filters) -> int:
    """Sesiones evaluadas que cerraron <10min y sin resolver (★<=2). Respeta filtros."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_FAST_CLOSE_SQL.format(where=where), params)
    return int(cur.fetchone()[0])


def summary_kpis(cur, account: str, **filters) -> dict:
    """KPIs agregados en la BD para el filtro dado (reemplaza el cómputo en memoria).
    Incluye `pendientes` (backfill en curso) y `cierres_rapidos` (señal diagnóstica)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_SUMMARY_KPIS_SQL.format(where=where), params)
    cols = [d.name for d in cur.description]
    row = cur.fetchone()
    kpis = {c: _coerce(v) for c, v in zip(cols, row)}
    kpis["pendientes"] = pending_sessions_count(
        cur, account, filters.get("date_from"), filters.get("date_to")
    )
    kpis["cierres_rapidos"] = fast_close_count(cur, account, **filters)
    return kpis


# Mapeo label->estrella y orden de buckets, igual que RATINGS/ORDER del front.
# Los labels de bot (optima/funcional/mejorable/falla) caen en el mismo bucket
# que su equivalente humano por estrella.
_LABEL_STARS = {"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1,
                "optima": 5, "funcional": 4, "mejorable": 3, "falla": 1}
_ORDER = ["excelente", "buena", "aceptable", "deficiente", "mala"]


def _dist_from_labels(rows) -> dict:
    """{bucket: count} desde filas (rating_label, n). bucket = ORDER[5-estrella]."""
    counts = {l: 0 for l in _ORDER}
    for label, n in rows:
        s = _LABEL_STARS.get(label)
        if s:
            counts[_ORDER[5 - s]] += int(n)
    return counts


# Distribución (renderDist): cuenta por bucket de estrella sobre las evaluadas.
# OJO: usa los filtros MENOS 'rating' (populationForDist), para mostrar todas las
# barras aunque haya una calificación seleccionada.
_DIST_SQL = """
SELECT cs.rating_label, count(*) AS n""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated' AND cs.rating_label IS NOT NULL
 GROUP BY cs.rating_label"""


def distribution(cur, account: str, **filters) -> dict:
    """Distribución de calificaciones por bucket (ignora el filtro 'rating')."""
    where, params = _scores_filters(account, **{**filters, "rating": "all"})
    cur.execute(_DIST_SQL.format(where=where), params)
    return _dist_from_labels(cur.fetchall())


def _build_ops(rows) -> list[dict]:
    """Tabla de operadores (renderOps) desde filas (op, rating_label, n, sum_stars):
    por operador -> volumen, ★ promedio y distribución por bucket. Orden por volumen."""
    by: dict[str, dict] = {}
    for op, label, n, sum_stars in rows:
        o = by.setdefault(op, {"name": op, "n": 0, "_sum": 0.0, "buckets": {l: 0 for l in _ORDER}})
        o["n"] += int(n)
        o["_sum"] += float(sum_stars or 0)
        if label in o["buckets"]:            # segmenta por label (igual que el front)
            o["buckets"][label] += int(n)
    out = []
    for o in by.values():
        out.append({"name": o["name"], "n": o["n"],
                    "avg": o["_sum"] / o["n"] if o["n"] else 0.0,
                    "dist": [o["buckets"][l] for l in _ORDER]})
    out.sort(key=lambda x: (-x["n"], x["name"]))
    return out


# Operadores: solo filas EVALUADAS y CON operador (user_name o user_id). Las filas
# sin nombre pero con user_id caen en 'Operador sin identificar' (como opName).
_OPS_SQL = """
SELECT coalesce(nullif(coalesce(u.name, cs.user_name), ''), 'Operador sin identificar') AS op,
       cs.rating_label, count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated'
   AND (u.name IS NOT NULL OR nullif(cs.user_name, '') IS NOT NULL OR cs.user_id IS NOT NULL)
 GROUP BY 1, cs.rating_label"""


def operators_table(cur, account: str, **filters) -> list[dict]:
    """Tabla de operadores agregada en la BD (reemplaza renderOps sobre DATA)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_OPS_SQL.format(where=where), params)
    return _build_ops(cur.fetchall())


# ★ por operador Y motivo (matriz): la vara JUSTA tras el refactor. El ★ global de un
# operador mezcla motivos con pisos distintos (transaccional=3 vs soporte); segmentado
# por motivo se compara peras con peras. Solo evaluadas y con operador.
_OPS_MOTIVO_SQL = """
SELECT coalesce(nullif(coalesce(u.name, cs.user_name), ''), 'Operador sin identificar') AS op,
       coalesce(cs.motivo, 'sin_motivo') AS motivo,
       count(*) AS n, avg(cs.stars) AS avg_stars""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated'
   AND (u.name IS NOT NULL OR nullif(cs.user_name, '') IS NOT NULL OR cs.user_id IS NOT NULL)
 GROUP BY 1, 2"""


def _build_ops_motivo(rows, top_n: int = 10) -> dict:
    """{motivos:[...], operators:[{name, n, cells:{motivo:{n,avg}}}]}. Top-N por volumen.
    Filas: (op, motivo, n, avg_stars)."""
    by: dict = {}
    for op, motivo, n, avg in rows:
        o = by.setdefault(op, {"name": op, "n": 0, "cells": {}})
        o["n"] += int(n)
        o["cells"][motivo] = {"n": int(n), "avg": _coerce(avg)}
    ops = sorted(by.values(), key=lambda x: (-x["n"], x["name"]))[:top_n]
    motivos = sorted({m for o in ops for m in o["cells"]})
    return {"motivos": motivos, "operators": ops}


def operators_by_motivo(cur, account: str, **filters) -> dict:
    """Matriz ★ por operador y motivo (respeta filtros). Agrega conversation_scores."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_OPS_MOTIVO_SQL.format(where=where), params)
    return _build_ops_motivo(cur.fetchall())


def _build_dep_channel(rows) -> list[dict]:
    """% depósito por canal (renderDepByChannel) desde filas (canal, n, dep)."""
    out = [{"canal": c, "n": int(n), "dep": int(dep), "pct": round(100 * int(dep) / int(n)) if n else 0}
           for c, n, dep in rows]
    out.sort(key=lambda x: (-x["n"], x["canal"]))
    return out


_DEP_CH_SQL = """
SELECT coalesce(t.channel, '—') AS canal, count(*) AS n,
       count(*) FILTER (WHERE cs.deposit_count > 0) AS dep""" + _SCORES_JOINS + """
 GROUP BY 1"""


def deposit_by_channel(cur, account: str, **filters) -> list[dict]:
    """% depósito por canal agregado en la BD (respeta filtros, incl. rating)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_DEP_CH_SQL.format(where=where), params)
    return _build_dep_channel(cur.fetchall())


# Evolución de la calidad por operador (renderQualityEvolution): ★ promedio por
# mes, top-N operadores por volumen, mes-operador con <min_conv -> None (ruido).
# Respeta filtros (a diferencia de los otros 3 cuadros full-scale de /api/charts).
def _build_quality_evolution(rows, top_n: int = 8, min_conv: int = 5) -> dict:
    """{months, operators:[{name, data:[★prom|None por mes]}]} desde filas
    (mes, op, n, sum_stars). Puro/testeable."""
    by: dict[str, dict] = {}
    for mes, op, n, sum_stars in rows:
        by.setdefault(op, {})[mes] = [float(sum_stars or 0), int(n)]
    months = sorted({m for ms in by.values() for m in ms})
    totals = {op: sum(v[1] for v in ms.values()) for op, ms in by.items()}
    top = sorted(totals, key=lambda o: (-totals[o], o))[:top_n]
    operators = []
    for op in top:
        data = []
        for m in months:
            c = by[op].get(m)
            data.append(round(c[0] / c[1], 2) if c and c[1] >= min_conv else None)
        operators.append({"name": op, "data": data})
    return {"months": months, "operators": operators}


_QUALITY_SQL = """
SELECT to_char(cs.conversation_created_at, 'YYYY-MM') AS mes,
       coalesce(nullif(coalesce(u.name, cs.user_name), ''), 'Operador sin identificar') AS op,
       count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated' AND cs.conversation_created_at IS NOT NULL
   AND (u.name IS NOT NULL OR nullif(cs.user_name, '') IS NOT NULL OR cs.user_id IS NOT NULL)
 GROUP BY 1, 2"""


def quality_evolution(cur, account: str, **filters) -> dict:
    """Evolución mensual de la ★ promedio por operador (respeta filtros)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_QUALITY_SQL.format(where=where), params)
    return _build_quality_evolution(cur.fetchall())


# Evolución de la ★ por MOTIVO (no por operador): responde "¿la calidad de depósito
# baja?, ¿soporte mejora?" en el tiempo. Reusa _build_quality_evolution (la 2da columna
# pasa a ser el motivo en vez del operador). Solo evaluadas.
_QUALITY_MOTIVO_SQL = """
SELECT to_char(cs.conversation_created_at, 'YYYY-MM') AS mes,
       coalesce(cs.motivo, 'sin_motivo') AS motivo,
       count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated' AND cs.conversation_created_at IS NOT NULL
 GROUP BY 1, 2"""


def quality_by_motivo_month(cur, account: str, **filters) -> dict:
    """Evolución mensual de la ★ promedio por MOTIVO (respeta filtros). {months, operators}
    donde cada 'operator' es en realidad un motivo (reusa _build_quality_evolution)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_QUALITY_MOTIVO_SQL.format(where=where), params)
    return _build_quality_evolution(cur.fetchall())


# Calidad por MOTIVO (v2). Clave tras el refactor: el ★ promedio GLOBAL mezcla motivos
# con varas distintas (un depósito en su piso=3 y un info bien=3-4) y se aplana hacia 3
# por el volumen transaccional. Segmentar por motivo devuelve una lectura honesta.
_MOTIVO_STATS_SQL = """
SELECT coalesce(cs.motivo, 'sin_motivo') AS motivo,
       count(*) AS n,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated') AS evaluadas,
       avg(cs.stars) FILTER (WHERE cs.eval_status = 'evaluated') AS avg_stars""" + _SCORES_JOINS + """
 GROUP BY 1"""


def _build_motivo_stats(rows) -> list[dict]:
    """[{motivo, n, evaluadas, avg}] ordenado por volumen. avg None si no hay evaluadas."""
    out = [{"motivo": m, "n": int(n), "evaluadas": int(ev), "avg": _coerce(avg)}
           for m, n, ev, avg in rows]
    out.sort(key=lambda x: -x["n"])
    return out


def motivo_stats(cur, account: str, **filters) -> list[dict]:
    """Volumen + ★ promedio por MOTIVO (respeta filtros). Agrega conversation_scores."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_MOTIVO_STATS_SQL.format(where=where), params)
    return _build_motivo_stats(cur.fetchall())


def summary(cur, account: str, **filters) -> dict:
    """Todos los agregados de las tarjetas/cuadros filtro-aware en una llamada: KPIs,
    distribución, tabla de operadores, % depósito por canal, evolución de calidad y
    calidad por motivo (v2). Reemplaza el cómputo en memoria sobre /api/scores."""
    return {
        "kpis": summary_kpis(cur, account, **filters),
        "distribution": distribution(cur, account, **filters),
        "operators": operators_table(cur, account, **filters),
        "deposit_by_channel": deposit_by_channel(cur, account, **filters),
        "quality_evolution": quality_evolution(cur, account, **filters),
        "motivo_stats": motivo_stats(cur, account, **filters),
        "ops_motivo": operators_by_motivo(cur, account, **filters),
        "quality_motivo": quality_by_motivo_month(cur, account, **filters),
    }


# --- B2 slice 3: lista de tickets paginada en el server. Una tarjeta = una PERSONA
# (contact_id), con sus conversaciones anidadas (renderTickets). Antes el front
# agrupaba/ordenaba/paginaba en memoria sobre las 113k filas; ahora la BD agrupa por
# tarjeta, ordena, y devuelve SOLO la página pedida + sus conversaciones.
DEFAULT_PAGE_SIZE = 12

# Clave de tarjeta: contact_id (persona) o fallback a ticket/conversación si el score
# quedó huérfano. Mismo criterio que el front (k = "c"+contact_id : "t"+...).
# conversation_id y ticket_id son uuid -> hay que castear AMBOS a text: COALESCE
# exige tipos homogéneos y COALESCE(text, uuid) revienta con "cannot be matched".
_CARD_KEY = ("CASE WHEN t.contact_id IS NOT NULL THEN 'c' || t.contact_id::text "
             "ELSE 't' || COALESCE(cs.ticket_id::text, cs.conversation_id::text) END")

# Orden de tarjetas = tks.sort del front. avg NULL (sin evaluar) siempre al final.
_TICKET_SORT = {
    "new":   "last_at DESC",
    "old":   "last_at ASC",
    "best":  "avg_stars DESC NULLS LAST",
    "worst": "avg_stars ASC NULLS LAST",
}

_TICKETS_CARDS_SQL = """
WITH pop AS (
  SELECT cs.conversation_id, cs.ticket_id, cs.stars, cs.eval_status,
         cs.conversation_created_at, t.channel,
         ct.name AS customer_name, ct.number AS customer_number,
         """ + _CARD_KEY + """ AS card_key
    FROM conversation_scores cs
    LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
    LEFT JOIN contacts ct ON ct.id = t.contact_id
    LEFT JOIN users    u  ON u.id  = cs.user_id
   WHERE {where}
)
SELECT card_key,
       count(*) AS n,
       count(DISTINCT ticket_id) AS visitas,
       avg(stars) FILTER (WHERE eval_status = 'evaluated') AS avg_stars,
       max(conversation_created_at) AS last_at,
       max(customer_name) AS cust,
       max(customer_number) AS num,
       (array_agg(channel ORDER BY conversation_created_at DESC NULLS LAST))[1] AS ch,
       count(*) OVER () AS total
  FROM pop
 GROUP BY card_key
 ORDER BY {order}
 LIMIT %(limit)s OFFSET %(offset)s"""

_TICKETS_CONVS_SQL = """
SELECT """ + _CARD_KEY + """ AS card_key,
       cs.conversation_id, cs.ticket_id, cs.conversation_created_at, cs.eval_status,
       cs.skip_reason, cs.rating_label, cs.stars,
       left(cs.rating_rationale, 160) AS rating_rationale,
       cs.rating_applicable, cs.atencion, cs.motivo,
       COALESCE(u.name, cs.user_name) AS user_name, cs.user_id
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE {where} AND (""" + _CARD_KEY + """) = ANY(%(keys)s)"""


def _sort_convs(convs: list[dict], sort: str) -> list[dict]:
    """Ordena las conversaciones de una tarjeta como sortConvs del front.
    Estrella None -> 99 (igual para best y worst, cuirco del front)."""
    if sort == "old":
        return sorted(convs, key=lambda c: c["conversation_created_at"] or "")
    if sort in ("best", "worst"):
        return sorted(convs, key=lambda c: c["stars"] if c["stars"] is not None else 99,
                      reverse=(sort == "best"))
    return sorted(convs, key=lambda c: c["conversation_created_at"] or "", reverse=True)


def _ticket_cards(card_rows: list[dict], conv_rows: list[dict], sort: str) -> list[dict]:
    """Arma las tarjetas (ya ordenadas y paginadas por SQL) con sus conversaciones
    agrupadas por card_key y ordenadas según el sort activo."""
    by_key: dict[str, list] = {}
    for cv in conv_rows:
        by_key.setdefault(cv["card_key"], []).append(cv)
    cards = []
    for cr in card_rows:
        cards.append({
            "key": cr["card_key"],
            "cust": cr["cust"], "num": cr["num"], "ch": cr["ch"],
            "n": cr["n"], "visitas": cr["visitas"],
            "avg": cr["avg_stars"], "last": cr["last_at"],
            "convs": _sort_convs(by_key.get(cr["card_key"], []), sort),
        })
    return cards


def tickets_page(cur, account: str, page: int = 1, sort: str = "new",
                 page_size: int = DEFAULT_PAGE_SIZE, **filters) -> dict:
    """Una página de tarjetas (persona + conversaciones), agrupada/ordenada/paginada
    en la BD. Reemplaza renderTickets sobre DATA completo."""
    where, params = _scores_filters(account, **filters)
    order = _TICKET_SORT.get(sort, _TICKET_SORT["new"])
    page = max(1, int(page))
    cur.execute(_TICKETS_CARDS_SQL.format(where=where, order=order),
                {**params, "limit": page_size, "offset": (page - 1) * page_size})
    card_rows = _rows_as_dicts(cur)
    total = int(card_rows[0]["total"]) if card_rows else 0
    keys = [c["card_key"] for c in card_rows]
    conv_rows: list[dict] = []
    if keys:
        cur.execute(_TICKETS_CONVS_SQL.format(where=where), {**params, "keys": keys})
        conv_rows = _rows_as_dicts(cur)
    pages = max(1, -(-total // page_size))
    return {"cards": _ticket_cards(card_rows, conv_rows, sort), "total": total,
            "page": page, "pages": pages, "page_size": page_size}


def filter_options(cur, account: str) -> dict:
    """Valores para los desplegables de filtros (segmento, canal, operador) de UNA
    cuenta, sin filtrar (el front los derivaba de DATA; ahora salen del server).
    Estable por cuenta -> el front lo pide una vez, no en cada cambio de filtro."""
    cur.execute("SELECT DISTINCT segment FROM conversation_scores "
                "WHERE account = %(account)s AND segment IS NOT NULL ORDER BY 1",
                {"account": account})
    segments = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT t.channel FROM conversation_scores cs "
                "JOIN tickets t ON t.id = cs.ticket_id "
                "WHERE cs.account = %(account)s AND t.channel IS NOT NULL ORDER BY 1",
                {"account": account})
    channels = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT coalesce(nullif(coalesce(u.name, cs.user_name), ''), "
                "'Operador sin identificar') AS op FROM conversation_scores cs "
                "LEFT JOIN users u ON u.id = cs.user_id WHERE cs.account = %(account)s "
                "AND (u.name IS NOT NULL OR nullif(cs.user_name, '') IS NOT NULL "
                "OR cs.user_id IS NOT NULL) ORDER BY 1",
                {"account": account})
    operators = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT motivo FROM conversation_scores "
                "WHERE account = %(account)s AND motivo IS NOT NULL ORDER BY 1",
                {"account": account})
    motivos = [r[0] for r in cur.fetchall()]
    return {"segments": segments, "channels": channels, "operators": operators,
            "motivos": motivos}


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


# =====================================================================
# Conversión jugador potencial -> jugador. Agrega la tabla player_conversions
# (precomputada por el pase determinista de src/conversions.py; 1 fila/persona).
# Filtrable por canal/segmento/operador/fecha de ENTRADA (first_at = cohorte por
# mes; la conversión es first-touch). NO usa estado/rating (no aplican al potencial).
# Operador = user_id (entidad users); NULL = bot/sin asignar.
# =====================================================================
_CONV_OP_EXPR = ("CASE WHEN pc.user_id IS NULL THEN 'BOT / sin operador' "
                 "ELSE coalesce(nullif(u.name, ''), 'Operador sin identificar') END")


def _conversion_where(account: str, *, canal="all", segment="all", op="all",
                      date_from=None, date_to=None, **_ignored) -> tuple[str, dict]:
    """(where, params) sobre player_conversions. Ignora filtros que no aplican al
    potencial (estado/rating/búsqueda). fecha = first_at (mes de entrada)."""
    where = ["pc.account = %(account)s"]
    params: dict = {"account": account}
    if canal and canal != "all":
        where.append("pc.channel = %(canal)s"); params["canal"] = canal
    if segment and segment != "all":
        where.append("pc.segment = %(segment)s"); params["segment"] = segment
    if op and op != "all":
        where.append(f"({_CONV_OP_EXPR}) = %(op)s"); params["op"] = op
    if date_from:
        where.append("pc.first_at >= %(dfrom)s"); params["dfrom"] = date_from
    if date_to:
        where.append("pc.first_at <= %(dto)s"); params["dto"] = date_to
    return " AND ".join(where), params


_CONV_BY_OP_SQL = """
SELECT """ + _CONV_OP_EXPR + """ AS op,
       count(*) AS potential,
       count(*) FILTER (WHERE pc.deposited) AS converted,
       count(*) FILTER (WHERE pc.returned) AS returned
  FROM player_conversions pc
  LEFT JOIN users u ON u.id = pc.user_id
 WHERE {where}
 GROUP BY 1"""


def _build_conversion_ranking(rows, min_potential: int = 8) -> dict:
    """Ranking por operador (op, potential, converted, returned): tasa de DEPÓSITO desc.
    `converted` = depositó; `returned` = re-engagement (volvió, >=2 sesiones). Bot en
    barra aparte; operadores con <min_potential se agregan en 'Otros'. Totales globales."""
    BOT = "BOT / sin operador"
    pct = lambda p, c: round(100.0 * c / p, 1) if p else 0.0
    bot = None
    top = []
    otros_p = otros_c = otros_r = 0
    tot_p = tot_c = tot_r = 0

    def _row(op, p, c, r):
        return {"op": op, "potential": p, "converted": c, "pct": pct(p, c),
                "returned": r, "ret_pct": pct(p, r)}

    for op, p, c, r in rows:
        p, c, r = int(p), int(c), int(r)
        tot_p += p; tot_c += c; tot_r += r
        if op == BOT:
            bot = _row(op, p, c, r)
        elif p < min_potential:
            otros_p += p; otros_c += c; otros_r += r
        else:
            top.append(_row(op, p, c, r))
    top.sort(key=lambda x: (-x["pct"], -x["potential"], x["op"]))
    if otros_p:
        top.append(_row("Otros", otros_p, otros_c, otros_r))
    if bot:
        top.append(bot)
    return {"operators": top, "total_potential": tot_p, "total_converted": tot_c,
            "pct": pct(tot_p, tot_c), "total_returned": tot_r, "ret_pct": pct(tot_p, tot_r)}


def conversion_by_operator(cur, account: str, **filters) -> dict:
    """Tasa de conversión por operador (ranking) + totales. Agrega player_conversions."""
    where, params = _conversion_where(account, **filters)
    cur.execute(_CONV_BY_OP_SQL.format(where=where), params)
    return _build_conversion_ranking(cur.fetchall())


_CONV_BY_MONTH_SQL = """
SELECT to_char(pc.first_at, 'YYYY-MM') AS mes,
       count(*) AS potential,
       count(*) FILTER (WHERE pc.deposited) AS converted,
       count(*) FILTER (WHERE pc.returned) AS returned
  FROM player_conversions pc
  LEFT JOIN users u ON u.id = pc.user_id
 WHERE {where} AND pc.first_at IS NOT NULL
 GROUP BY 1"""


def _build_conversion_by_month(rows) -> dict:
    """{months, potential[], converted[], pct[], returned[], ret_pct[]} por mes. Puro.
    converted/pct = depósito; returned/ret_pct = re-engagement (volvió)."""
    rows = sorted(rows, key=lambda r: r[0])
    _pct = lambda n, p: round(100.0 * n / p, 1) if p else 0.0
    months = [r[0] for r in rows]
    potential = [int(r[1]) for r in rows]
    converted = [int(r[2]) for r in rows]
    returned = [int(r[3]) for r in rows]
    return {"months": months, "potential": potential,
            "converted": converted, "pct": [_pct(c, p) for p, c in zip(potential, converted)],
            "returned": returned, "ret_pct": [_pct(r, p) for p, r in zip(potential, returned)]}


def conversion_by_month(cur, account: str, **filters) -> dict:
    """Jugadores nuevos y convertidos por mes de entrada (cuadro). Agrega player_conversions."""
    where, params = _conversion_where(account, **filters)
    cur.execute(_CONV_BY_MONTH_SQL.format(where=where), params)
    return _build_conversion_by_month(cur.fetchall())


# Cuadro del análisis: conversión vs atención pasiva por operador/mes (small-multiples).
# conv% = depositó / total (siempre conocido). pasiva% = pasivo / CLASIFICADAS (attention
# no NULL), NO sobre el total: attention se llena de a poco (pase LLM) y no queremos
# diluir la línea roja con lo aún sin clasificar. Solo operadores HUMANOS (user_id).
_CONV_PASV_SQL = """
SELECT to_char(pc.first_at, 'YYYY-MM') AS mes,
       coalesce(nullif(u.name, ''), 'Operador sin identificar') AS op,
       count(*) AS n,
       count(*) FILTER (WHERE pc.deposited) AS conv,
       count(*) FILTER (WHERE pc.attention IS NOT NULL) AS clasif,
       count(*) FILTER (WHERE pc.attention = 'pasivo') AS pasiva
  FROM player_conversions pc
  JOIN users u ON u.id = pc.user_id
 WHERE {where} AND pc.first_at IS NOT NULL AND pc.user_id IS NOT NULL
 GROUP BY 1, 2"""


def _build_conversion_passivity(rows, top_n: int = 8, min_conv: int = 5) -> dict:
    """{months, operators:[{name, conv:[%|None], pasiva:[%|None]}]} para el cuadro
    verde(conv)/rojo(pasiva) por operador. conv% sobre total; pasiva% sobre clasificadas.
    Top-N operadores por volumen; mes-operador con <min_conv -> None (rompe la línea)."""
    by: dict[str, dict] = {}
    for mes, op, n, conv, clasif, pasiva in rows:
        by.setdefault(op, {})[mes] = (int(n), int(conv), int(clasif), int(pasiva))
    months = sorted({m for ms in by.values() for m in ms})
    totals = {op: sum(v[0] for v in ms.values()) for op, ms in by.items()}
    top = sorted(totals, key=lambda o: (-totals[o], o))[:top_n]
    operators = []
    for op in top:
        conv_s, pasv_s = [], []
        for m in months:
            c = by[op].get(m)
            if c and c[0] >= min_conv:
                conv_s.append(round(100.0 * c[1] / c[0], 1))
            else:
                conv_s.append(None)
            if c and c[2] >= min_conv:              # clasif >= min
                pasv_s.append(round(100.0 * c[3] / c[2], 1))
            else:
                pasv_s.append(None)
        operators.append({"name": op, "conv": conv_s, "pasiva": pasv_s})
    return {"months": months, "operators": operators}


def conversion_passivity_evolution(cur, account: str, **filters) -> dict:
    """Evolución mensual conv% vs pasiva% por operador (cuadro del análisis)."""
    where, params = _conversion_where(account, **filters)
    cur.execute(_CONV_PASV_SQL.format(where=where), params)
    return _build_conversion_passivity(cur.fetchall())


# Drill-down: la cohorte de jugadores nuevos de un operador (o filtro) con las
# llaves para abrir su conversación de entrada. Responde "¿qué pasó?" -> a los
# mensajes. El operador clickeado llega como filtro `op` (via _conversion_where).
_CONV_COHORT_SQL = """
SELECT pc.contact_id, pc.first_conversation_id, pc.first_at, pc.channel, pc.deposited
  FROM player_conversions pc
  LEFT JOIN users u ON u.id = pc.user_id
 WHERE {where}
 ORDER BY pc.first_at DESC
 LIMIT 500"""


def conversion_cohort(cur, account: str, **filters) -> list[dict]:
    """Personas (jugadores nuevos) de la cohorte filtrada, con first_conversation_id
    para el drill-down al modal de conversación. Tope 500 (la UI pagina/scrollea)."""
    where, params = _conversion_where(account, **filters)
    cur.execute(_CONV_COHORT_SQL.format(where=where), params)
    return _rows_as_dicts(cur)


def conversation_detail(cur, conversation_id: str) -> dict | None:
    """Una conversacion con su analisis completo + transcript reconstruido.

    Si NO hay fila de score (sesion pendiente / aun no scoreada), igual devolvemos el
    CHAT desde los mensajes con `pending=True`, para que el drill de cohorte (u otro)
    pueda ABRIR la conversacion aunque el worker todavia no la haya evaluado. Devuelve
    None solo si tampoco hay mensajes (no hay nada que mostrar)."""
    cur.execute(_DETAIL_SQL, {"cid": conversation_id})
    row = cur.fetchone()
    if row:
        cols = [d.name for d in cur.description]
        d = {c: _coerce(v) for c, v in zip(cols, row)}
        d["transcript"] = _transcript(fetch_messages(cur, conversation_id))
        return d
    transcript = _transcript(fetch_messages(cur, conversation_id))
    if not transcript:
        return None
    return {"conversation_id": conversation_id, "eval_status": None,
            "pending": True, "transcript": transcript}
