"""Sesionizacion: agrupa episodios (conversations) de un mismo ticket en sesiones.

La unidad de evaluacion es la SESION (decision D1 del diseno,
docs/diseno-evaluacion-unificada.md). Recorriendo los episodios de un ticket por
created_at se CORTA (nueva sesion) cuando el episodio PREVIO CERRO (su ultimo
mensaje del agente matchea una senal de cierre: confirmacion de carga / despedida
/ diferido, regex CLOSING), o CAMBIO el agente humano (agentes dominantes no nulos
y distintos), o el gap entre consecutivos supera GAP, o el span de la sesion
superaria SPAN_CAP. Se MERGEA solo cuando el previo NO cerro, mismo (o sin) agente,
gap <= GAP y dentro del span. Un episodio solo-cliente (sin agente, sin cierre)
mergea con el siguiente -> mata el skip fabricado.

La regla vive entera en la funcion PURA assign_sessions (unit-testeable sin BD).
refresh_account_sessions la aplica full-scale por cuenta y materializa el resultado
(idempotente, self-healing como player_conversions).

Mapeo episodio->sesion: SEGUNDA tabla conversation_session_map (conversation_id PK
-> session_id), no una columna en conversation_sessions. Motivo: conversation_sessions
tiene grano SESION (una fila por sesion, PK (account, session_id)); el mapeo tiene
grano EPISODIO (una fila por conversation). Meterlos en la misma tabla romperia esa
PK; dos tablas mantienen cada grano en su lugar y siguen el patron de conversions.py.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta

from src.metrics import message_stats
from src.router import decide_eligibility, decide_rubric

GAP = timedelta(hours=5)
SPAN_CAP = timedelta(hours=12)  # una sesion no puede abarcar mas que esto

# CLOSING: el agente CERRO la interaccion en su ultimo mensaje del episodio
# (confirmacion de carga, despedida, o diferido "me avisas"). Si matchea, la
# interaccion termino -> el siguiente episodio arranca sesion nueva. Un episodio
# solo-cliente no tiene last_agent_body -> None -> NO cierra -> mergea. (regex
# validada durante el analisis; no reescribir sin re-validar contra datos reales.)
CLOSING = re.compile(
    r"(saldo\s+ya\s+est|carga\s+.*acredit|ya\s+(le|te)\s+carg|cargad[oa]|"
    r"recarga\s+ya\s+est|ya\s+est[aá]\s+tu\s+saldo|"
    r"[eé]xitos|mucha\s+suerte|a\s+la\s+orden|un\s+(gusto|placer)|"
    r"cuando\s+(puedas|quieras|tengas\s+tiempo|gustes|desees)|apenas\s+puedas|"
    r"me\s+avis|me\s+escrib|aqu[ií]\s+est|quedo\s+atent|estamos\s+disponibles|"
    r"estar[eé]\s+pendiente|estoy\s+pendiente|cualquier\s+(cosa|duda|consulta)|no\s+dudes)",
    re.IGNORECASE,
)


def assign_sessions(episodes: list[dict]) -> list[dict]:
    """Asigna cada episodio de UN ticket a su sesion (regla D1). PURA, sin BD.

    episodes: lista de dicts {conversation_id, created_at, last_agent_body, agent_id}
    de un mismo ticket. last_agent_body = ultimo mensaje del agente de ese episodio
    (o None si no hubo); agent_id = agente humano DOMINANTE de ese episodio (o None).

    Devuelve lista de dicts {conversation_id, sess_no, session_id}. sess_no arranca en
    0 por ticket; session_id = conversation_id del PRIMER episodio de esa (ticket,
    sess_no).

    Corta (nueva sesion) cuando el episodio PREVIO cerro (CLOSING), o cambio el agente
    humano dominante (ambos no nulos y distintos), o el gap con el previo supera GAP,
    o el span desde el inicio de la sesion actual superaria SPAN_CAP. Merge en caso
    contrario. Un episodio solo-cliente (sin agente, sin cierre) mergea con el siguiente.

    Ordena internamente por (created_at, conversation_id): no depende de que el caller
    la pase ordenada y desempata determinísticamente los created_at iguales (mismo
    criterio que el tiebreaker del SQL que la alimenta).
    """
    episodes = sorted(episodes, key=lambda e: (e["created_at"], str(e["conversation_id"])))
    result: list[dict] = []
    sess_no = 0
    session_id = None
    session_start = None
    prev = None
    for ep in episodes:
        if prev is None:
            sess_no = 0
            session_id = ep["conversation_id"]
            session_start = ep["created_at"]
        else:
            gap = ep["created_at"] - prev["created_at"]
            prev_closed = bool(CLOSING.search(prev.get("last_agent_body") or ""))
            a_prev, a_cur = prev.get("agent_id"), ep.get("agent_id")
            agent_changed = a_prev is not None and a_cur is not None and a_prev != a_cur
            span_exceeded = (ep["created_at"] - session_start) > SPAN_CAP
            if prev_closed or agent_changed or gap > GAP or span_exceeded:
                sess_no += 1
                session_id = ep["conversation_id"]
                session_start = ep["created_at"]
            # merge -> misma sesion (no se toca sess_no, session_id ni session_start)
        result.append({
            "conversation_id": ep["conversation_id"],
            "sess_no": sess_no,
            "session_id": session_id,
        })
        prev = ep
    return result


def evaluate_session(messages: list[dict]):
    """Stats + rubrica + elegibilidad sobre el transcript MERGEADO de la sesion. PURA.

    Espeja los pasos deterministas del scorer por conversacion (src/worker.py
    score_and_store) pero a grano SESION: recibe TODOS los mensajes de todos los
    episodios (ya mergeados en orden cronologico global) y reusa TAL CUAL las
    funciones puras existentes -> no las reimplementa.

    Devuelve (stats, rubric, eval_status, skip_reason). Es lo que elimina los skips
    fabricados: si el agente respondio en un episodio hermano, el transcript
    mergeado tiene agent_message_count>0 y decide_eligibility devuelve 'evaluated'
    en vez de un falso 'no_agent_reply'.

    No calcula deposito ni operador ni corre el LLM: eso es la pieza 3.
    """
    stats = message_stats(messages)
    rubric = decide_rubric(
        agent_message_count=stats.agent_message_count,
        bot_message_count=stats.bot_message_count,
    )
    eval_status, skip_reason = decide_eligibility(
        real_message_count=stats.message_count,
        customer_message_count=stats.contact_message_count,
        business_message_count=stats.agent_message_count + stats.bot_message_count,
        customer_text_count=stats.contact_text_message_count,
    )
    return stats, rubric, eval_status, skip_reason


# Idempotente + self-healing (como conversions.ensure_table): el pase las asegura al
# correr. conversation_sessions = grano sesion; conversation_session_map = grano
# episodio (mapeo conversation_id -> session_id).
_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS conversation_sessions (
        account       text        NOT NULL,
        ticket_id     uuid        NOT NULL,
        session_id    uuid        NOT NULL,
        sess_no       int         NOT NULL,
        start_at      timestamptz,
        end_at        timestamptz,
        episode_count int,
        PRIMARY KEY (account, session_id)
    )""",
    """
    CREATE TABLE IF NOT EXISTS conversation_session_map (
        conversation_id uuid NOT NULL,
        account         text NOT NULL,
        session_id      uuid NOT NULL,
        PRIMARY KEY (conversation_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_sessions_ticket ON conversation_sessions (account, ticket_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_sessions_sid    ON conversation_sessions (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_sess_map_sid    ON conversation_session_map (account, session_id)",
)

# Ultimo body del agente por conversacion (from_me, sin nota, no vacio) -> 1 query,
# scopeado por cuenta.
# Tiebreaker `id DESC`: sin el, dos mensajes del agente con el mismo created_at
# (comun en cargas por lote) hacen que DISTINCT ON elija uno no determinista.
_LAST_AGENT_SQL = """
SELECT DISTINCT ON (conversation_id) conversation_id, body
  FROM messages
 WHERE account = %(account)s AND from_me = true AND is_note = false
   AND body IS NOT NULL AND length(trim(body)) > 0
 ORDER BY conversation_id, created_at DESC, id DESC
"""

# Agente humano DOMINANTE por conversacion (el user_id con mas mensajes propios,
# sin notas) -> para detectar cambio de agente entre episodios. row_number sobre el
# conteo por (conversation_id, user_id) y se toma el rn=1 de cada conversacion.
_PRIMARY_AGENT_SQL = """
SELECT conversation_id, user_id
  FROM (
    SELECT conversation_id, user_id,
           row_number() OVER (PARTITION BY conversation_id ORDER BY count(*) DESC) AS rn
      FROM messages
     WHERE account = %(account)s AND from_me = true AND is_note = false
       AND user_id IS NOT NULL
     GROUP BY conversation_id, user_id
  ) s
 WHERE rn = 1
"""

# Episodios de la cuenta ordenados por ticket y created_at. Tiebreaker `id ASC`:
# garantiza orden estable entre corridas cuando dos conversaciones del mismo ticket
# comparten created_at (si no, el session_id/sess_no podria variar entre refreshes).
_CONVERSATIONS_SQL = """
SELECT ticket_id, id, created_at
  FROM conversations
 WHERE account = %(account)s AND ticket_id IS NOT NULL
 ORDER BY ticket_id, created_at ASC, id ASC
"""

_SESS_UPSERT = """
INSERT INTO conversation_sessions
      (account, ticket_id, session_id, sess_no, start_at, end_at, episode_count)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (account, session_id) DO UPDATE
   SET ticket_id     = EXCLUDED.ticket_id,
       sess_no       = EXCLUDED.sess_no,
       start_at      = EXCLUDED.start_at,
       end_at        = EXCLUDED.end_at,
       episode_count = EXCLUDED.episode_count
"""

_MAP_UPSERT = """
INSERT INTO conversation_session_map (conversation_id, account, session_id)
VALUES (%s, %s, %s)
ON CONFLICT (conversation_id) DO UPDATE
   SET account    = EXCLUDED.account,
       session_id = EXCLUDED.session_id
"""

# Limpieza de huerfanas: si al recomputar cambian las fronteras (redeploy que toca
# GAP/CLOSING, o datos historicos), un session_id que dejo de ser inicio-de-sesion
# quedaria como fila muerta en conversation_sessions (el UPSERT nunca la borra). El
# mapeo (grano episodio) siempre queda correcto, asi que una sesion sin NINGUN episodio
# que la apunte es huerfana -> se borra. Quirurgico: en steady-state no borra nada.
_ORPHAN_DELETE = """
DELETE FROM conversation_sessions cs
 WHERE cs.account = %(account)s
   AND NOT EXISTS (
     SELECT 1 FROM conversation_session_map m
      WHERE m.account = cs.account AND m.session_id = cs.session_id)
"""


def ensure_sessions_table(cur) -> None:
    """Crea conversation_sessions + conversation_session_map + indices (idempotente)."""
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)


def refresh_account_sessions(cur, account: str) -> int:
    """Recomputa TODAS las sesiones de una cuenta (full-scale) y las materializa.

    Trae las conversaciones con ticket_id, el ultimo body del agente y el agente
    humano dominante por conversacion, arma los episodios por ticket, aplica
    assign_sessions y hace UPSERT en conversation_sessions (grano sesion) +
    conversation_session_map (grano episodio). Idempotente. Devuelve la cantidad de
    sesiones materializadas.
    """
    ensure_sessions_table(cur)
    cur.execute(_LAST_AGENT_SQL, {"account": account})
    last_agent = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(_PRIMARY_AGENT_SQL, {"account": account})
    primary_agent = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(_CONVERSATIONS_SQL, {"account": account})
    rows = cur.fetchall()

    # Agrupar episodios por ticket (rows ya vienen ordenados por ticket_id, created_at).
    by_ticket: dict = defaultdict(list)
    for ticket_id, conv_id, created_at in rows:
        by_ticket[ticket_id].append({
            "conversation_id": conv_id,
            "created_at": created_at,
            "last_agent_body": last_agent.get(conv_id),
            "agent_id": primary_agent.get(conv_id),
        })

    sess_rows: list[tuple] = []
    map_rows: list[tuple] = []
    for ticket_id, episodes in by_ticket.items():
        assigned = assign_sessions(episodes)
        agg: dict = {}  # session_id -> agregados de la sesion
        for ep, a in zip(episodes, assigned):
            sid = a["session_id"]
            map_rows.append((a["conversation_id"], account, sid))
            g = agg.get(sid)
            if g is None:
                agg[sid] = {"sess_no": a["sess_no"], "start_at": ep["created_at"],
                            "end_at": ep["created_at"], "count": 1}
            else:  # episodios asc: el ultimo visto es el end_at
                g["end_at"] = ep["created_at"]
                g["count"] += 1
        for sid, g in agg.items():
            sess_rows.append((account, ticket_id, sid, g["sess_no"],
                              g["start_at"], g["end_at"], g["count"]))

    if sess_rows:
        cur.executemany(_SESS_UPSERT, sess_rows)
    if map_rows:
        cur.executemany(_MAP_UPSERT, map_rows)
    # Barrer sesiones huerfanas de la cuenta (fronteras que cambiaron entre corridas).
    cur.execute(_ORPHAN_DELETE, {"account": account})
    return len(sess_rows)
