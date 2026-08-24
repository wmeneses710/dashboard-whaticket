"""Sesionizacion: agrupa episodios (conversations) de un mismo ticket en sesiones.

La unidad de evaluacion es la SESION (decision D1 del diseno,
docs/diseno-evaluacion-unificada.md). Recorriendo los episodios de un ticket por
created_at se CORTA (nueva sesion) cuando el episodio PREVIO CERRO (su ultimo
mensaje del operador matchea una senal de cierre: confirmacion de carga / despedida
/ diferido, regex CLOSING), o CAMBIO el operador humano (operadores dominantes no nulos
y distintos), o el SILENCIO entre consecutivos (ultimo mensaje del previo, con techo en
su resolved_at -> primer mensaje del siguiente) supera GAP, o el span de la sesion
superaria SPAN_CAP. Se MERGEA solo cuando el previo NO cerro, mismo (o sin) operador,
silencio <= GAP y dentro del span. Un episodio solo-cliente (sin operador, sin cierre)
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
from src.signals import client_sin_motivo, operator_resolved

GAP = timedelta(hours=5)
SPAN_CAP = timedelta(hours=12)  # una sesion no puede abarcar mas que esto

# CLOSING: el operador CERRO la interaccion en su ultimo mensaje del episodio
# (confirmacion de carga, despedida, o diferido "me avisas"). Si matchea, la
# interaccion termino -> el siguiente episodio arranca sesion nueva. Un episodio
# solo-cliente no tiene last_operator_body -> None -> NO cierra -> mergea. (regex
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


def _actividad(ep: dict, clave: str):
    """first_at/last_at del episodio; si no vienen, created_at.

    Un episodio sin mensajes reales (solo notas, o vacio) no tiene ventana de actividad:
    ahi created_at es lo unico que hay. Mismo fallback que usa el materializado de
    start_at/end_at, asi el gap y la ventana nunca discrepan sobre el mismo episodio.
    """
    return ep.get(clave) or ep["created_at"]


def _fin_de_actividad(ep: dict):
    """last_at con TECHO en resolved_at. SOLO para el gap, NO para el end_at materializado.

    POR QUE EL TECHO. El ETL archiva mensajes en episodios ya cerrados: el mensaje que
    deberia ABRIR el episodio siguiente queda pegado al previo. Entonces last_at del previo
    se va al futuro, queda ~igual al first_at del siguiente, el silencio sale ~0 y se
    mergean dos interacciones genuinamente separadas. Medido en whaticket_copia: 15 casos
    en `datos` (32% de sus flips) y 33 fronteras sucias en total. Un episodio no puede
    tener actividad despues de haberse cerrado, asi que el techo lo elimina por
    construccion en vez de estadisticamente.

    POR QUE SOLO EN EL GAP. last_at hace DOS trabajos que no necesitan el mismo valor:
      1. el GAP -> pregunta "termino la interaccion?"  -> ACA SI va el techo
      2. el end_at materializado -> pregunta "ya puedo evaluar esto?" -> ACA NO
    El end_at tiene que seguir avanzando con el ultimo mensaje real: de eso depende que la
    sesion se re-abra y se re-scoree cuando el operador contesta tarde (si no, queda un
    falso 'no_agent_reply' permanente). Ver _LAST_MSG_SQL.

    Sin resolved_at (episodio abierto) no hay techo. No se pone piso en created_at porque
    no hace falta: verificado en whaticket_copia, 0 de 152.894 conversaciones resueltas
    tienen resolved_at anterior a su created_at.
    """
    fin = _actividad(ep, "last_at")
    resuelto = ep.get("resolved_at")
    return resuelto if resuelto is not None and fin > resuelto else fin


def assign_sessions(episodes: list[dict]) -> list[dict]:
    """Asigna cada episodio de UN ticket a su sesion (regla D1). PURA, sin BD.

    episodes: lista de dicts {conversation_id, created_at, last_operator_body, operator_id}
    de un mismo ticket, opcionalmente con {first_at, last_at} = ventana de actividad
    (primer y ultimo mensaje real del episodio). last_operator_body = ultimo mensaje del
    operador de ese episodio (o None si no hubo); operator_id = operador humano DOMINANTE
    de ese episodio (o None).

    Devuelve lista de dicts {conversation_id, sess_no, session_id}. sess_no arranca en
    0 por ticket; session_id = conversation_id del PRIMER episodio de esa (ticket,
    sess_no).

    Corta (nueva sesion) cuando el episodio PREVIO cerro (CLOSING), o cambio el operador
    humano dominante (ambos no nulos y distintos), o el SILENCIO con el previo supera GAP,
    o el span desde el inicio de la sesion actual superaria SPAN_CAP. Merge en caso
    contrario. Un episodio solo-cliente (sin operador, sin cierre) mergea con el siguiente.

    EL GAP SE MIDE POR INACTIVIDAD, no entre created_at. El gap pregunta "termino la
    interaccion?" y eso lo contesta el silencio real: ultimo mensaje del previo -> primer
    mensaje del siguiente. created_at es cuando NACIO el episodio y no se mueve cuando
    llegan mensajes, asi que medir nacimiento contra nacimiento cortaba y mergeaba en los
    momentos equivocados (dos episodios nacidos cerca con actividad separada por dias, y
    al reves). Si el previo sigue activo cuando el siguiente arranca, el silencio es
    negativo: nunca supera GAP -> mergea, que es lo correcto.

    EL last_at DEL PREVIO LLEVA TECHO EN SU resolved_at (ver _fin_de_actividad): sin el,
    un mensaje archivado en un episodio ya cerrado cierra el silencio artificialmente y
    mergea dos interacciones separadas. El techo aplica SOLO al gap; el end_at
    materializado sigue usando el ultimo mensaje real.

    El SPAN sigue sobre created_at a proposito: es un techo de seguridad para que una
    cadena de merges no crezca sin limite, no una medicion de actividad. Pasarlo a
    actividad esta pendiente de que el ETL arregle la atribucion de messages.conversation_id
    (hoy la primera conversacion de cada ticket absorbe mensajes de años antes, asi que un
    span por actividad cortaria por datos mal atribuidos, no por la interaccion real).

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
            gap = _actividad(ep, "first_at") - _fin_de_actividad(prev)
            prev_closed = bool(CLOSING.search(prev.get("last_operator_body") or ""))
            a_prev, a_cur = prev.get("operator_id"), ep.get("operator_id")
            operator_changed = a_prev is not None and a_cur is not None and a_prev != a_cur
            span_exceeded = (ep["created_at"] - session_start) > SPAN_CAP
            if prev_closed or operator_changed or gap > GAP or span_exceeded:
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


def evaluate_session(messages: list[dict], lineas: dict | None = None,
                     es_grupo: bool | None = False,
                     linea_propia: str | None = None):
    """Stats + rubrica + elegibilidad sobre el transcript MERGEADO de la sesion. PURA.

    `lineas`: mapa tail-de-9-digitos -> status de `connections` (ver
    src/redireccion.build_lineas_map), para decidir el skip por `redireccion`. Sin el
    mapa NO se skipea nada por traspaso: falla del lado seguro.

    `es_grupo`: `tickets.is_group`. Un grupo de WhatsApp no es una atencion uno-a-uno y no
    se califica (ver src/router.decide_eligibility y tests/test_grupo_de_whatsapp.py). El
    default es False por la misma razon que `lineas`: sin el dato no se saltea nada.

    Espeja los pasos deterministas del scorer por conversacion (src/worker.py
    score_and_store) pero a grano SESION: recibe TODOS los mensajes de todos los
    episodios (ya mergeados en orden cronologico global) y reusa TAL CUAL las
    funciones puras existentes -> no las reimplementa.

    Devuelve (stats, rubric, eval_status, skip_reason). Es lo que elimina los skips
    fabricados: si el operador respondio en un episodio hermano, el transcript
    mergeado tiene operator_message_count>0 y decide_eligibility devuelve 'evaluated'
    en vez de un falso 'no_agent_reply'.

    No calcula deposito ni operador ni corre el LLM: eso es la pieza 3.
    """
    stats = message_stats(messages)
    rubric = decide_rubric(
        operator_message_count=stats.operator_message_count,
        bot_message_count=stats.bot_message_count,
    )
    eval_status, skip_reason = decide_eligibility(
        real_message_count=stats.message_count,
        customer_message_count=stats.contact_message_count,
        business_message_count=stats.operator_message_count + stats.bot_message_count,
        customer_text_count=stats.contact_text_message_count,
        operator_resolved=operator_resolved(messages),
        es_grupo=es_grupo,
    )
    # `sin motivo`: el cliente nunca planteo nada (todo lo suyo es saludo o acuse).
    # Va DESPUES de decide_eligibility a proposito, por dos razones: necesita los
    # mensajes (decide_eligibility solo ve contadores), y los skips previos son
    # informacion mas util — saber que el negocio nunca respondio explica mejor la
    # sesion que saber que el cliente solo dijo "hola".
    # `sin_motivo` NO SE APLICA CUANDO NADIE RESPONDIO, y esa prioridad viene del negocio:
    # "si no hubo respuesta del negocio, ese caso manda -- es informacion mas util que
    # 'sin_motivo' para entender por que no se califico". Hasta el 2026-08-21 la prioridad se
    # cumplia sola porque `no_agent_reply` era un skip y ganaba antes en `decide_eligibility`.
    # Al dejar de serlo, `sin_motivo` se comia justo esas sesiones -- volvian a desaparecer,
    # ahora etiquetadas como que el cliente no planteo nada, cuando lo que paso es que no le
    # contestaron. El guard reconstruye el orden explicito.
    # `sin_motivo` YA NO SE SALTEA (decision del negocio, 2026-08-21). Eran **5.247**
    # sesiones que desaparecian del denominador: el tablero medaba sobre menos sesiones de las
    # que hubo. Ahora se evalua por el ESTANDAR DE CIERRE, que es lo unico que el manual pide
    # cuando el cliente no planteo nada -- ver src/solo_cortesia.py. El worker rutea usando
    # `client_sin_motivo` directo.
    # EL GUARD DE `hubo_negocio` YA NO HACE FALTA aca: nadie-respondio tambien se evalua
    # (src/sin_respuesta.py) y el worker le da prioridad porque lo chequea primero. La
    # prioridad del negocio -- "si no hubo respuesta, ese caso manda" -- se conserva en el
    # ORDEN del worker, que es donde ahora vive la decision.
    # `redireccion` YA NO SE SALTEA (decision del negocio, 2026-08-20): es un motivo con
    # nota determinista propia (src/redireccion.score_redireccion). El skip protegia bien
    # -- el traspaso puro caia en 2 estrellas por "no atendio el motivo" -- pero BORRABA el
    # traspaso del tablero, y el negocio lo quiere contar. La proteccion ahora vive en la
    # rubrica, que da 4 cuando la linea de destino esta viva.
    # `sin_motivo` sigue ganandole (bucket A, decision del 2026-08-07): si el cliente
    # tampoco planteo nada, la etiqueta que queda es `sin_motivo`. Por eso el chequeo de
    # arriba no se toco y este bloque desaparecio en vez de moverse.
    # `redireccion` NO se decide aca. Vuelve a ser skip cuando el destino esta vivo
    # (decision del negocio, 2026-08-24), pero eso vive en el ORDEN del worker: la cortesia
    # le gana al traspaso (decision del 2026-08-07) y aca correria ANTES de ese chequeo.
    # Ver src/worker.py y src/redireccion.traspaso_limpio.
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

# Ultimo body del operador por conversacion (from_me, sin nota, no vacio) -> 1 query,
# scopeado por cuenta.
# Tiebreaker `id DESC`: sin el, dos mensajes del operador con el mismo created_at
# (comun en cargas por lote) hacen que DISTINCT ON elija uno no determinista.
_LAST_AGENT_SQL = """
SELECT DISTINCT ON (conversation_id) conversation_id, body
  FROM messages
 WHERE account = %(account)s AND from_me = true AND is_note = false
   AND body IS NOT NULL AND length(trim(body)) > 0
 ORDER BY conversation_id, created_at DESC, id DESC
"""

# Operador humano DOMINANTE por conversacion (el user_id con mas mensajes propios,
# sin notas) -> para detectar cambio de operador entre episodios. row_number sobre el
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
SELECT ticket_id, id, created_at, resolved_at
  FROM conversations
 WHERE account = %(account)s AND ticket_id IS NOT NULL
 ORDER BY ticket_id, created_at ASC, id ASC
"""

# Ventana REAL de actividad por conversacion: primer y ultimo mensaje (sin notas).
# El start_at/end_at materializado sale de ACA, NO de conversations.created_at.
# POR QUE: conversations.created_at es cuando NACIO la conversacion; no se mueve cuando
# llegan mensajes nuevos. Si end_at fuera ese valor, el gate del worker
# (end_at < now()-6h + re-open por scored_at) (1) scorearia conversaciones aun ACTIVAS
# 6h despues de su inicio, con transcript parcial, y (2) nunca las re-abriria al llegar
# la respuesta del operador -> falso 'no_agent_reply' permanente. Con el ultimo mensaje,
# end_at avanza y la sesion se re-abre para re-scorear (auto-sanante).
_LAST_MSG_SQL = """
SELECT conversation_id, min(created_at) AS first_at, max(created_at) AS last_at
  FROM messages
 WHERE account = %(account)s AND is_note = false
 GROUP BY conversation_id
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

    Trae las conversaciones con ticket_id, el ultimo body del operador y el operador
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
    cur.execute(_LAST_MSG_SQL, {"account": account})
    # conv_id -> (primer_msg, ultimo_msg). Falta la conv si no tiene mensajes reales.
    msg_times = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute(_CONVERSATIONS_SQL, {"account": account})
    rows = cur.fetchall()

    # Agrupar episodios por ticket (rows ya vienen ordenados por ticket_id, created_at).
    by_ticket: dict = defaultdict(list)
    for ticket_id, conv_id, created_at, resolved_at in rows:
        # first_at/last_at = ventana de actividad del episodio. Van al episodio (no solo
        # al agregado) porque assign_sessions las necesita para medir el silencio real
        # entre episodios; None si la conversacion no tiene mensajes reales.
        first_at, last_at = msg_times.get(conv_id, (None, None))
        by_ticket[ticket_id].append({
            "conversation_id": conv_id,
            "created_at": created_at,
            # Techo del gap (ver _fin_de_actividad). NO afecta el end_at materializado.
            "resolved_at": resolved_at,
            "first_at": first_at,
            "last_at": last_at,
            "last_operator_body": last_agent.get(conv_id),
            "operator_id": primary_agent.get(conv_id),
        })

    sess_rows: list[tuple] = []
    map_rows: list[tuple] = []
    for ticket_id, episodes in by_ticket.items():
        assigned = assign_sessions(episodes)
        agg: dict = {}  # session_id -> agregados de la sesion
        for ep, a in zip(episodes, assigned):
            sid = a["session_id"]
            map_rows.append((a["conversation_id"], account, sid))
            # Ventana de actividad del episodio = primer/ultimo MENSAJE real (no el
            # created_at de la conversacion). Fallback al created_at si la conversacion
            # no tiene mensajes reales. min(start)/max(end) sobre los episodios, sin
            # depender del orden (el ultimo episodio no siempre trae el ultimo mensaje).
            first_at = _actividad(ep, "first_at")
            last_at = _actividad(ep, "last_at")
            g = agg.get(sid)
            if g is None:
                agg[sid] = {"sess_no": a["sess_no"], "start_at": first_at,
                            "end_at": last_at, "count": 1}
            else:
                if first_at < g["start_at"]:
                    g["start_at"] = first_at
                if last_at > g["end_at"]:
                    g["end_at"] = last_at
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
