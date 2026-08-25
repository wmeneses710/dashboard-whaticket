"""Capa de lectura para el dashboard (account-scoped).

REGLA: datos y sistemas conviven en la MISMA base. Toda lectura de scores exige
`account` en el WHERE, para que el dashboard traiga una cuenta u otra segun lo
seleccionado. El transcript se pide aparte (on-demand) porque es pesado.
"""
from __future__ import annotations

from decimal import Decimal

from src.context import fetch_messages, fetch_session_messages
from src.identidad import (HAY_OPERADOR, OPERADOR_O_NADA, OPERADOR_RESUELTO,
                          clave_sql, expr_resuelto)
from src.router import ANOMALOUS_MESSAGE_MAX
from src.rubrics import MOTIVOS

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
       cs.user_id, """ + OPERADOR_O_NADA + """ AS user_name,
       cs.conversation_created_at,
       cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       left(cs.rating_rationale, 160) AS rating_rationale, cs.deposit_count,
       t.contact_id AS contact_id,
       ct.name AS customer_name, ct.number AS customer_number, t.channel
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE {where}
 ORDER BY cs.conversation_created_at DESC
"""

_DETAIL_SQL = """
SELECT cs.conversation_id, cs.ticket_id, cs.account, cs.segment, cs.queue_name,
       cs.user_id, """ + OPERADOR_O_NADA + """ AS user_name,
       cs.conversation_created_at, cs.resolved_at,
       cs.rubric, cs.eval_status, cs.skip_reason, cs.rating_label, cs.stars,
       cs.rating_rationale, cs.deposit_count, cs.dimensions, cs.message_count, cs.agent_message_count,
       cs.bot_message_count, cs.contact_message_count, cs.first_response_seconds,
       cs.resolution_seconds, cs.was_unassigned, cs.scoring_version, cs.llm_model,
       cs.atencion, cs.deposit_observed, cs.deposit_mismatch, cs.motivo,
       ct.name AS customer_name, ct.number AS customer_number, t.channel,
       pc.returned AS conversion_returned,
       EXTRACT(EPOCH FROM (ses.end_at - ses.start_at)) AS session_seconds,
       -- LA PUERTA: cuanto espero el operador entre su ultima accion y el cierre del
       -- ticket. Medido el 2026-08-06: mediana 0,0 min y 4 de cada 5 sesiones cierran
       -- en menos de un minuto, o sea que casi nadie deja margen para una segunda duda.
       -- (Sin el signo de porcentaje en los comentarios: psycopg parsea el SQL
       -- entero buscando placeholders y uno suelto revienta en runtime.)
       EXTRACT(EPOCH FROM (cs.resolved_at - (
           SELECT max(m.created_at) FROM messages m
            WHERE m.conversation_id = cs.conversation_id
              AND m.from_me AND NOT m.is_note
       ))) AS cierre_seconds,
       -- Y cuanto espero DESPUES de chequear si al cliente le faltaba algo. El patron
       -- se pasa como parametro desde src.signals: fuente unica con la senal que usan
       -- las rubricas, para que no se desincronicen.
       EXTRACT(EPOCH FROM (cs.resolved_at - (
           SELECT max(m.created_at) FROM messages m
            WHERE m.conversation_id = cs.conversation_id
              AND m.from_me AND NOT m.is_note
              AND m.body ~* %(algo_mas_re)s
       ))) AS algo_mas_cierre_seconds
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


# COLUMNAS QUE LLEVAN UN TELEFONO Y SALEN AL FRONT. `customer_number` viaja en cuatro
# consultas (lista, detalle y las dos de tarjetas) y `num` es su agregado por tarjeta.
# `_transcript` ya enmascara lo que va DENTRO del chat; sin esto quedaba el mismo dato
# expuesto por la puerta de al lado, en un dominio publico con endpoints anonimos.
# EL NOMBRE NO ESTA ACA a proposito: taparlo es una decision de producto pendiente del
# negocio (el del OPERADOR es el eje del tablero), no un arreglo de seguridad.
_COLUMNAS_CON_TELEFONO = ("customer_number", "num")


def _rows_as_dicts(cur) -> list[dict]:
    """Las filas como dicts, con el telefono ENMASCARADO.

    Es el punto UNICO por donde pasan las cinco consultas que devuelven filas, asi que la
    censura vive aca una sola vez. Va en la SALIDA y no en el SQL: `_CARD_KEY` agrupa por
    `contact_id`/`ticket_id` (uuid, nunca el telefono) y el front devuelve esas claves para
    la segunda consulta, asi que el dato crudo tiene que seguir disponible del lado de la
    base. Ver src/censura.py para por que nada de esto puede pasar antes de calificar.
    """
    from src.censura import censurar_texto
    cols = [d.name for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = {c: _coerce(v) for c, v in zip(cols, r)}
        for c in _COLUMNAS_CON_TELEFONO:
            # `censurar_texto` devuelve "" para None, y un None tiene que seguir siendo
            # None: el front distingue "sin telefono" de "telefono tapado".
            if d.get(c):
                d[c] = censurar_texto(str(d[c]))
        out.append(d)
    return out


def list_accounts(cur) -> list[str]:
    """Cuentas presentes en la tabla de scores (para el selector)."""
    cur.execute(
        "SELECT account, count(*) FROM conversation_scores "
        "WHERE account IS NOT NULL GROUP BY account ORDER BY account"
    )
    return [{"account": a, "count": n} for a, n in cur.fetchall()]


def scored_rows(cur, account: str, **filters) -> list[dict]:
    """Conversaciones scoreadas de UNA cuenta (sin transcript), con los filtros aplicados.

    Antes ignoraba todo filtro y devolvía la cuenta entera. Con el switch de ambiente eso
    dejaba de servir: la lista mezclaba jugadores, agentes y sin clasificar sin recorte
    posible. Comparte el WHERE con el resto del tablero (`_scores_filters`), así que la
    baja lógica de operadores también aplica acá."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_SCORES_SQL.format(where=where), params)
    return _rows_as_dicts(cur)


# --- B2: agregados server-side. En vez de mandar las ~113k filas al cliente para
# que agregue en memoria (~112MB/13s), la BD calcula los KPIs/distribución/ops y
# devuelve unos KB. Los filtros del front (matchBase) se traducen a un WHERE común.
# 'rating' bucketea por estrella (excelente=5 ... mala=1), igual que bucketOf.
_RATING_STARS = {"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1}


# CLAVE de comparacion de nombres de operador: minusculas y sin tildes.
# `operator_status` matcheaba por STRING EXACTO, y cuando la canonicalizacion por persona
# (2026-08-07) eligio la grafia dominante, dos apagados quedaron sin efecto:
#     'OnlySorti' guardado -> 'onlysorti' resuelto
#     'Anahi'     guardado -> 'Anahí'     resuelto   (31.626 mensajes, volvia a aparecer)
# Se compara por clave en vez de renombrar las filas: renombrar arregla esos dos y deja la
# trampa armada para la proxima grafia que cambie.
# translate() y NO unaccent(): unaccent es una EXTENSION que puede no estar instalada en la
# base de produccion, y esto tiene que andar sin pedir superusuario.
# La regla de identidad vive en src/identidad.py (fuente unica). Ver ahi el por que.
_clave_sql = clave_sql
_OPERADOR_RESUELTO = OPERADOR_RESUELTO
_OPERADOR_O_NADA = OPERADOR_O_NADA


# BAJA LÓGICA de operadores. Un operador apagado desaparece de TODO lo que sale de
# conversation_scores — KPIs incluidos, no solo de los cuadros por operador: en `sistemas`
# son 31 operadores que ya no trabajan aportando 27.398 sesiones históricas que ensuciaban
# promedios y rankings.
#
# NOT EXISTS y no una lista de nombres traída desde Python: así `_scores_filters` sigue
# siendo puro (account + kwargs -> where, params) y no necesita un cursor. La subconsulta va
# contra una tabla diminuta con PK (account, operator_name).
#
# El default es ESCONDER, pero la baja es lógica: `inactivos="incluir"` los trae de vuelta.
# Sin esa salida, apagar a alguien sería una eliminación de hecho.
_SIN_APAGADOS = f"""NOT EXISTS (
     SELECT 1 FROM operator_status os
      WHERE os.account = cs.account AND os.activo = false
        AND {_clave_sql('os.operator_name')} = {_clave_sql(_OPERADOR_RESUELTO)})"""


def _scores_filters(account: str, *, estado="all", segment="all", canal="all",
                    op="all", date_from=None, date_to=None, rating="all",
                    search="", motivo="all", inactivos="ocultar",
                    ambiente="todos", causa="all") -> tuple[str, dict]:
    """(where_sql, params) para conversation_scores, replicando matchBase del front.
    Los valores van SIEMPRE como parámetros (%(...)s); el SQL solo arma columnas.

    ORDEN DE LOS FILTROS (jerarquía del negocio, 2026-08-07): manda OPERADORES —
    `inactivos`, la baja lógica— y adentro de eso el AMBIENTE. Por eso los dos van
    primero. `segment` sigue vivo como filtro FINO adentro del ambiente (p. ej. aislar
    `marketing` dentro de sin_clasificar): los dos componen con AND."""
    from src.segments import segments_for_ambiente

    where = ["cs.account = %(account)s"]
    params: dict = {"account": account}
    if inactivos != "incluir":
        where.append(_SIN_APAGADOS)
    if ambiente and ambiente != "todos":
        # `todos` NO agrega predicado a propósito: `cs.segment` no es NULL en ninguna fila
        # (0 de 130.558 medidas), así que sin filtro y con la lista completa dan lo mismo,
        # y sin filtro es una condición menos que planificar.
        where.append("cs.segment = ANY(%(amb_segments)s)")
        params["amb_segments"] = list(segments_for_ambiente(ambiente))
    if estado and estado != "all":
        where.append("cs.eval_status = %(estado)s"); params["estado"] = estado
    # CAUSA de sin evaluar (2026-08-14). Existe para que la tarjeta "Sin evaluar, por causa"
    # sea CLICABLE: hasta ahora no lo era, y el front lo documentaba — "el filtro de estado
    # es Todas/Evaluadas/Sin evaluar y no sabe de causas, asi que un clic mostraria TODO lo
    # salteado y no la fila apretada".
    # NO hace falta tocar `estado`: las filas evaluadas tienen `skip_reason` NULL, asi que
    # este predicado ya las deja afuera solo.
    if causa and causa != "all":
        where.append("cs.skip_reason = %(causa)s"); params["causa"] = causa
    if motivo and motivo != "all":
        where.append("cs.motivo = %(motivo)s"); params["motivo"] = motivo
    if segment and segment != "all":
        where.append("cs.segment = %(segment)s"); params["segment"] = segment
    if canal and canal != "all":
        where.append("t.channel = %(canal)s"); params["canal"] = canal
    if op and op != "all":
        where.append(f"{_OPERADOR_RESUELTO} = %(op)s"); params["op"] = op
    if date_from:
        where.append("cs.conversation_created_at >= %(dfrom)s"); params["dfrom"] = date_from
    if date_to:
        where.append("cs.conversation_created_at <= %(dto)s"); params["dto"] = date_to
    if rating and rating != "all" and rating in _RATING_STARS:
        where.append("cs.stars = %(rstars)s"); params["rstars"] = _RATING_STARS[rating]
    if search:
        where.append("(ct.name ILIKE %(q)s OR ct.number ILIKE %(q)s "
                     f"OR {_OPERADOR_RESUELTO} ILIKE %(q)s)")
        params["q"] = f"%{search}%"
    return " AND ".join(where), params


_SCORES_JOINS = """
  FROM conversation_scores cs
  LEFT JOIN tickets  t  ON t.id  = cs.ticket_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
  LEFT JOIN users    u  ON u.id  = cs.user_id
 WHERE {where}"""

# Sesiones descartadas de TODO agregado de depósito: las PATOLÓGICAS por tamaño, las que
# superan ANOMALOUS_MESSAGE_MAX (250) mensajes reales. En prod hay una de 15.100 mensajes
# con 3.025 imágenes del cliente. El router nunca las manda al LLM, pero `deposit_count` sí
# se calcula y persiste igual (es un gate determinista, independiente del eval_status — ver
# src/worker.py), así que se colaba en los KPIs: el 0,89% de las sesiones de `sistemas`
# aportaba el 44,7% de los comprobantes (88.496 de 198.027), casi duplicando el número.
#
# Se filtra por la PROPIEDAD (message_count) y NO por `skip_reason='anomalous_size'`: en
# decide_eligibility el chequeo de 'no_agent_reply' corre ANTES que el de tamaño, así que 16
# sesiones de >250 mensajes quedaron con la etiqueta equivocada y se escapaban del filtro
# con 2.310 comprobantes. La etiqueta es un efecto del orden de los ifs; el tamaño es el
# hecho. Verificado: 0 sesiones 'anomalous_size' con <=250 mensajes, así que este predicado
# es superset estricto del anterior.
#
# Los OTROS skips se conservan a propósito: `customer_media_only` es el cliente mandando el
# comprobante sin que nadie responda, y eso es un depósito real.
# coalesce defensivo: `message_count` es nullable en el DDL. Sin el coalesce, un NULL haría
# `NOT (NULL > 250)` = NULL y el FILTER descartaría esa fila EN SILENCIO del total de
# depósitos. Hoy no hay ningún NULL (0 de 126.347), y la idea es que siga sin importar.
_ANOMALA = f"coalesce(cs.message_count, 0) > {ANOMALOUS_MESSAGE_MAX}"
_NO_ANOMALA = f"NOT ({_ANOMALA})"

# KPIs = renderKpis del front: total, evaluadas, sin evaluar, ★ promedio (solo evaluadas),
# depósitos (suma), sesiones con depósito, operadores distintos (evaluadas), + lo EXCLUIDO
# por sesión anómala (para declararlo en la UI en vez de esconder el descarte).
_SUMMARY_KPIS_SQL = f"""
SELECT count(*) AS total,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated') AS evaluadas,
       count(*) FILTER (WHERE cs.eval_status <> 'evaluated') AS no_evaluadas,
       avg(cs.stars) FILTER (WHERE cs.eval_status = 'evaluated') AS avg_stars,
       coalesce(sum(cs.deposit_count) FILTER (WHERE {_NO_ANOMALA}), 0) AS depositos,
       count(*) FILTER (WHERE cs.deposit_count > 0 AND {_NO_ANOMALA}) AS dep_conv,
       count(DISTINCT coalesce(nullif(coalesce(u.name, cs.user_name), ''), cs.user_id::text))
             FILTER (WHERE cs.eval_status = 'evaluated') AS operadores,
       coalesce(sum(cs.deposit_count) FILTER (WHERE {_ANOMALA}), 0) AS depositos_excluidos,
       count(*) FILTER (WHERE {_ANOMALA} AND cs.deposit_count > 0) AS sesiones_excluidas,
       -- Lo que se esconde se DECLARA: cuántos operadores están apagados en esta cuenta.
       -- Si el dashboard oculta 31 operadores sin decirlo, los números mienten por omisión.
       (SELECT count(*) FROM operator_status os2
         WHERE os2.account = %(account)s AND os2.activo = false) AS operadores_ocultos""" + _SCORES_JOINS


# "Pendiente de evaluar" = sesión CERRADA (end_at < now-6h, misma condición que el
# worker en PENDING_SESSIONS_SQL) que todavía NO tiene score al día. Es la señal de
# "hay backfill en curso": el dashboard escaso no es un agujero, es proceso. Scopeado
# por cuenta + rango de fechas (sobre start_at); los otros filtros (segmento/rating)
# no aplican a lo aún-no-scoreado.
_PENDING_SESSIONS_COUNT_SQL = """
SELECT count(*) AS pendientes
  FROM conversation_sessions cs{join}
 WHERE cs.account = %(account)s
   AND cs.end_at < now() - interval '6 hours'
   AND NOT EXISTS (
     SELECT 1 FROM conversation_scores s
      WHERE s.session_id = cs.session_id AND s.scored_at >= cs.end_at)
   {date_clause}"""


def pending_sessions_count(cur, account: str, date_from=None, date_to=None,
                           ambiente="todos") -> int:
    """Sesiones cerradas de la cuenta que aún no fueron scoreadas (backfill en curso).

    Respeta el AMBIENTE: sin eso el tablero decía los mismos 112.187 pendientes mirando
    jugador, agente o sin_clasificar — un número sin origen al lado de los KPIs, que es
    exactamente lo que el switch viene a eliminar. `conversation_sessions` no tiene columna
    `segment`, así que el recorte pasa por la cola de la conversación (hash join, 24 ms
    medidos). En 'todos' no se paga el join."""
    params: dict = {"account": account}
    clause = ""
    join = ""
    if ambiente and ambiente != "todos":
        from src.segments import ambiente_incluye_sin_cola

        qids = _queue_ids_for_ambiente(cur, account, ambiente)
        sin_cola = ambiente_incluye_sin_cola(ambiente)
        if not qids and not sin_cola:
            return 0
        join = "\n  JOIN conversations c ON c.id = cs.session_id"
        clause += f" AND {_cola_pred(sin_cola)}"
        params["qids"] = qids
    if date_from:
        clause += " AND cs.start_at >= %(dfrom)s"; params["dfrom"] = date_from
    if date_to:
        clause += " AND cs.start_at <= %(dto)s"; params["dto"] = date_to
    cur.execute(_PENDING_SESSIONS_COUNT_SQL.format(date_clause=clause, join=join), params)
    return int(cur.fetchone()[0])


# "Cierre rápido": sesión EVALUADA que cerró muy rápido (<10min) Y sin resolver (★<=2).
# Señal DIAGNÓSTICA — la conversación concluyó rápido sin solucionar al usuario; puede
# ser deficiencia de configuración (auto-close agresivo), no siempre culpa del operador.
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


# "Discrepancia de depósito": el gate DETERMINISTA (comprobante del cliente) y la
# observación del LLM (deposit_observed) NO coinciden. Es un indicador de CALIDAD DE
# DATO: o el LLM se comió un comprobante, o el gate disparó de más. El determinista
# manda; el flag solo marca la discrepancia. Se computa en store._deposit_mismatch.
_DEPOSIT_MISMATCH_SQL = """
SELECT count(*) AS deposit_mismatch""" + _SCORES_JOINS + """
   AND cs.deposit_mismatch = true"""


def deposit_mismatch_count(cur, account: str, **filters) -> int:
    """Sesiones donde el gate determinista y la observación del LLM discrepan. Filtros."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_DEPOSIT_MISMATCH_SQL.format(where=where), params)
    return int(cur.fetchone()[0])


def summary_kpis(cur, account: str, **filters) -> dict:
    """KPIs agregados en la BD para el filtro dado (reemplaza el cómputo en memoria).
    Incluye `pendientes` (backfill en curso). Los diagnósticos del creador (cierres
    rápidos, discrepancia de depósito) NO se agregan acá: son consultables aparte
    (fast_close_count / deposit_mismatch_count) para no correr esos COUNT en cada carga."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_SUMMARY_KPIS_SQL.format(where=where), params)
    cols = [d.name for d in cur.description]
    row = cur.fetchone()
    kpis = {c: _coerce(v) for c, v in zip(cols, row)}
    kpis["pendientes"] = pending_sessions_count(
        cur, account, filters.get("date_from"), filters.get("date_to"),
        ambiente=filters.get("ambiente", "todos"),
    )
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


# Operadores: solo filas EVALUADAS y con RASTRO de un operador. `agent_message_count > 0`
# es la cuarta puerta y no es cosmetica: el guard viejo (u.name / user_name / user_id) tiraba
# del cuadro, EN SILENCIO, 640 sesiones donde un humano escribio y no lo pudimos nombrar.
# Que no sepamos quien fue no puede significar que el trabajo no exista. El solo-bot (35
# sesiones, sin un mensaje humano) sigue afuera: ahi no hubo operador que evaluar.
_OPS_SQL = f"""
SELECT {_OPERADOR_RESUELTO} AS op,
       cs.rating_label, count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + f"""
   AND cs.eval_status = 'evaluated'
   AND {HAY_OPERADOR}
 GROUP BY 1, cs.rating_label"""


def operators_table(cur, account: str, **filters) -> list[dict]:
    """Tabla de operadores agregada en la BD (reemplaza renderOps sobre DATA)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_OPS_SQL.format(where=where), params)
    return _build_ops(cur.fetchall())


# ★ por operador Y motivo (matriz): la vara JUSTA tras el refactor. El ★ global de un
# operador mezcla motivos con pisos distintos (transaccional=3 vs soporte); segmentado
# por motivo se compara peras con peras. Solo evaluadas y con operador.
_OPS_MOTIVO_SQL = f"""
SELECT {_OPERADOR_RESUELTO} AS op,
       coalesce(cs.motivo, 'sin_motivo') AS motivo,
       count(*) AS n, avg(cs.stars) AS avg_stars""" + _SCORES_JOINS + f"""
   AND cs.eval_status = 'evaluated'
   AND {HAY_OPERADOR}
 GROUP BY 1, 2"""


def _build_ops_motivo(rows, top_n: int | None = None) -> dict:
    """{motivos:[...], operators:[{name, n, cells:{motivo:{n,avg}}}]}. Filas: (op, motivo,
    n, avg_stars), ordenadas por volumen desc.

    `top_n=None` (default) = TODOS los operadores. Es una TABLA: una fila por operador, así
    que sumar operadores no degrada la legibilidad (scrollea dentro de la tarjeta). Antes
    topaba en 10 y en `sistemas` (50 operadores) escondía 40 sin decirlo.
    """
    by: dict = {}
    for op, motivo, n, avg in rows:
        o = by.setdefault(op, {"name": op, "n": 0, "cells": {}})
        o["n"] += int(n)
        o["cells"][motivo] = {"n": int(n), "avg": _coerce(avg)}
    ops = sorted(by.values(), key=lambda x: (-x["n"], x["name"]))
    if top_n is not None:
        ops = ops[:top_n]
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


_DEP_CH_SQL = f"""
SELECT coalesce(t.channel, '—') AS canal, count(*) AS n,
       count(*) FILTER (WHERE cs.deposit_count > 0 AND {_NO_ANOMALA}) AS dep""" + _SCORES_JOINS + """
 GROUP BY 1"""


def deposit_by_channel(cur, account: str, **filters) -> list[dict]:
    """% depósito por canal agregado en la BD (respeta filtros, incl. rating)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_DEP_CH_SQL.format(where=where), params)
    return _build_dep_channel(cur.fetchall())


# Evolución de la calidad por operador (renderQualityEvolution): ★ promedio por
# mes, top-N operadores por volumen, mes-operador con <min_conv -> None (ruido).
# Respeta filtros (a diferencia de los otros 3 cuadros full-scale de /api/charts).
def _build_quality_evolution(rows, top_n: int | None = None, min_conv: int = 5) -> dict:
    """{months, operators:[{name, n, data:[★prom|None por mes]}]} desde filas
    (mes, op, n, sum_stars). Puro/testeable.

    `top_n=None` (default) = TODOS los operadores, ordenados por volumen desc. Este cuadro
    dibuja UN mini-grafico POR operador, asi que agregar operadores no ensucia ningun
    grafico: solo agrega tarjetitas. El front decide cuantas muestra.

    `n` = sesiones evaluadas del operador (para ordenar/mostrar volumen en el front y para
    distinguir "no tiene datos" de "no llega al minimo mensual").
    """
    by: dict[str, dict] = {}
    for mes, op, n, sum_stars in rows:
        by.setdefault(op, {})[mes] = [float(sum_stars or 0), int(n)]
    months = sorted({m for ms in by.values() for m in ms})
    totals = {op: sum(v[1] for v in ms.values()) for op, ms in by.items()}
    ranked = sorted(totals, key=lambda o: (-totals[o], o))
    if top_n is not None:
        ranked = ranked[:top_n]
    operators = []
    for op in ranked:
        data = []
        for m in months:
            c = by[op].get(m)
            data.append(round(c[0] / c[1], 2) if c and c[1] >= min_conv else None)
        operators.append({"name": op, "n": totals[op], "data": data})
    return {"months": months, "operators": operators}


_QUALITY_SQL = f"""
SELECT to_char(cs.conversation_created_at, 'YYYY-MM') AS mes,
       {_OPERADOR_RESUELTO} AS op,
       count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + f"""
   AND cs.eval_status = 'evaluated' AND cs.conversation_created_at IS NOT NULL
   AND {HAY_OPERADOR}
 GROUP BY 1, 2"""


def quality_evolution(cur, account: str, **filters) -> dict:
    """Evolución mensual de la ★ promedio por operador (respeta filtros)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_QUALITY_SQL.format(where=where), params)
    return _build_quality_evolution(cur.fetchall())


# Orden canónico de motivos para presentar los gráficos (los conocidos primero, en el
# orden de la rúbrica; 'sin_motivo' y cualquier desconocido al final).
_MOTIVO_ORDER = {m: i for i, m in enumerate(MOTIVOS)}


def _build_quality_motivo(rows, top_n: int | None = None, op_min_conv: int = 3,
                          avg_min_conv: int = 5) -> dict:
    """{months, motivos:[{motivo, operators:[{name, n, data}], avg:[★|None], n_ops}]} desde
    filas (mes, motivo, op, n, sum_stars). PURO/testeable.

    Por cada motivo: una línea por OPERADOR (ordenados por volumen desc, mes con
    <op_min_conv -> None) + la línea de PROMEDIO del motivo (TODOS los operadores del
    motivo, mes con <avg_min_conv -> None). op_min_conv es más bajo que avg_min_conv porque
    al abrir por operador los conteos mensuales son chicos; el promedio agrega y aguanta un
    umbral mayor.

    OJO — a diferencia de _build_quality_evolution, acá todos los operadores comparten UN
    mini-gráfico por motivo: cada operador es una LÍNEA más. Con 50 operadores es spaghetti
    ilegible, así que el front SIEMPRE recorta y el corte tiene que ser visible. Por eso se
    devuelve `n_ops` = cuántos operadores tiene el motivo en total, para que el front pueda
    decir "8 de 37" en vez de mentir por omisión. `top_n=None` = sin recorte en el backend
    (el recorte real lo hace el front, que sabe cuánto espacio tiene).
    """
    by: dict[str, dict[str, dict[str, list]]] = {}
    # Guard en el builder ademas del SQL: `sin_motivo` es la AUSENCIA de motivo (sesiones de
    # agente, calificadas con agilidad). Si alguien vuelve a meter el coalesce en la query,
    # el cuadro no se rompe.
    rows = [r for r in rows if r[1] and r[1] != "sin_motivo"]
    for mes, motivo, op, n, sum_stars in rows:
        by.setdefault(motivo, {}).setdefault(op, {})[mes] = [float(sum_stars or 0), int(n)]
    months = sorted({mes for mo in by.values() for ops in mo.values() for mes in ops})
    motivos = []
    for motivo in sorted(by, key=lambda m: (_MOTIVO_ORDER.get(m, len(_MOTIVO_ORDER)), m)):
        ops = by[motivo]
        totals = {op: sum(v[1] for v in ms.values()) for op, ms in ops.items()}
        ranked = sorted(totals, key=lambda o: (-totals[o], o))
        if top_n is not None:
            ranked = ranked[:top_n]
        operators = []
        for op in ranked:
            data = [
                round(c[0] / c[1], 2) if (c := ops[op].get(m)) and c[1] >= op_min_conv else None
                for m in months
            ]
            operators.append({"name": op, "n": totals[op], "data": data})
        avg = []
        for m in months:
            s = sum(ms[m][0] for ms in ops.values() if m in ms)
            cnt = sum(ms[m][1] for ms in ops.values() if m in ms)
            avg.append(round(s / cnt, 2) if cnt >= avg_min_conv else None)
        # n_ops = operadores TOTALES del motivo (antes de cualquier recorte del front).
        motivos.append({"motivo": motivo, "operators": operators, "avg": avg,
                        "n_ops": len(totals)})
    return {"months": months, "motivos": motivos}


# Evolución de la ★ por MOTIVO abierta POR OPERADOR: cada motivo muestra una línea por
# usuario + el promedio del motivo. Responde "¿quién baja la calidad de depósito?, ¿quién
# lleva soporte?". Solo evaluadas; respeta filtros; mismo guard de operador que _QUALITY_SQL.
# `sin_motivo` NO entra: no es un motivo, es la ausencia de uno. Son las sesiones del
# segmento AGENTE, que se califican con la rubrica de agilidad y nunca pasan por la
# clasificacion de motivo (motivo NULL). En una tarjeta que compara la calidad ENTRE motivos
# meterlas es comparar peras con la falta de peras, y en `sistemas` son el grupo mas grande:
# arrastraban el promedio del cuadro. Decision del negocio, 2026-08-07.
_QUALITY_MOTIVO_SQL = f"""
SELECT to_char(cs.conversation_created_at, 'YYYY-MM') AS mes,
       cs.motivo AS motivo,
       {_OPERADOR_RESUELTO} AS op,
       count(*) AS n, sum(cs.stars) AS sum_stars""" + _SCORES_JOINS + f"""
   AND cs.eval_status = 'evaluated' AND cs.conversation_created_at IS NOT NULL
   AND cs.motivo IS NOT NULL
   AND {HAY_OPERADOR}
 GROUP BY 1, 2, 3"""


def quality_by_motivo_month(cur, account: str, **filters) -> dict:
    """Evolución mensual de la ★ por MOTIVO y OPERADOR (respeta filtros).
    {months, motivos:[{motivo, operators:[{name,data}], avg}]}."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_QUALITY_MOTIVO_SQL.format(where=where), params)
    return _build_quality_motivo(cur.fetchall())


# Calidad por MOTIVO (v2). Clave tras el refactor: el ★ promedio GLOBAL mezcla motivos
# con varas distintas (un depósito en su piso=3 y un info bien=3-4) y se aplana hacia 3
# por el volumen transaccional. Segmentar por motivo devuelve una lectura honesta.
# `sin_motivo` NO ENTRA, igual que en _QUALITY_MOTIVO_SQL: no es un motivo, es la AUSENCIA
# de uno (decision del negocio, 2026-08-07). Esta tarjeta se habia quedado afuera de ese
# cambio y seguia con un `coalesce(cs.motivo,'sin_motivo')`.
# PARECIA arreglado en `sistemas` de casualidad: ahi las filas sin motivo son casi todas del
# segmento `agente` (6.158 de 6.687) y el filtro de AMBIENTE ya las barria. En `datos`, que
# no tiene agente, las 712 filas son sesiones `jugador` SALTEADAS y el ambiente no las toca,
# asi que la fila seguia a la vista — de ahi la impresion de que el arreglo era por cuenta.
# Y el label mentia doble: de las 710 de `datos` solo 450 son `skip_reason='sin_motivo'`; las
# otras 260 son customer_media_only (188), no_agent_reply (51), anomalous_size (12) y demas.
# Era todo lo salteado en una bolsa con el nombre de una sola de sus causas.
_MOTIVO_STATS_SQL = """
SELECT cs.motivo AS motivo,
       count(*) AS n,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated') AS evaluadas,
       avg(cs.stars) FILTER (WHERE cs.eval_status = 'evaluated') AS avg_stars""" + _SCORES_JOINS + """
   AND cs.motivo IS NOT NULL
 GROUP BY 1"""


def _build_motivo_stats(rows) -> list[dict]:
    """[{motivo, n, evaluadas, avg}] ordenado por volumen. avg None si no hay evaluadas.

    Guard en el builder ademas del SQL (mismo patron que _build_quality_motivo): si alguien
    vuelve a meter el coalesce o saca el `IS NOT NULL`, la tarjeta no se rompe.
    """
    out = [{"motivo": m, "n": int(n), "evaluadas": int(ev), "avg": _coerce(avg)}
           for m, n, ev, avg in rows if m and m != "sin_motivo"]
    out.sort(key=lambda x: -x["n"])
    return out


def motivo_stats(cur, account: str, **filters) -> list[dict]:
    """Volumen + ★ promedio por MOTIVO (respeta filtros). Agrega conversation_scores."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_MOTIVO_STATS_SQL.format(where=where), params)
    return _build_motivo_stats(cur.fetchall())


# COBERTURA de la tarjeta de motivo. La tarjeta promedia SOLO lo que tiene motivo, y en
# `sistemas` eso es el 29% de lo evaluado (39 de 135 el 2026-08-12). Un promedio que no dice
# sobre que poblacion se calculo se lee como si cubriera todo.
# Y las dos causas de "sin motivo" van SEPARADAS porque no son lo mismo:
#  - `agente`: el motivo es NULL POR DISEÑO (esas se califican por agilidad, sin LLM). Es una
#    frontera, no una perdida. Hoy explica las 96.
#  - cualquier otra: el clasificador no emitio motivo en una sesion que SI debia tenerlo. Eso
#    es un bug, y por eso tiene su propio contador: mientras sea 0, no hay nada que buscar.
_MOTIVO_COBERTURA_SQL = """
SELECT count(*) FILTER (WHERE cs.eval_status = 'evaluated') AS evaluadas,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated'
                          AND cs.motivo IS NOT NULL) AS con_motivo,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated'
                          AND cs.motivo IS NULL AND cs.segment = 'agente') AS sin_agente,
       count(*) FILTER (WHERE cs.eval_status = 'evaluated'
                          AND cs.motivo IS NULL
                          AND cs.segment IS DISTINCT FROM 'agente') AS sin_otro""" \
    + _SCORES_JOINS


def _build_motivo_cobertura(row) -> dict:
    ev, con, sin_ag, sin_otro = (int(x or 0) for x in row)
    if con + sin_ag + sin_otro != ev:
        # Los tres cajones parten lo evaluado sin solaparse. Si dejan de cerrar es que alguien
        # toco un FILTER, y entonces el porcentaje habla de una poblacion que ya no existe.
        raise ValueError(f"la cobertura de motivo no cierra: {con}+{sin_ag}+{sin_otro} != {ev}")
    return {"evaluadas": ev, "con_motivo": con, "sin_motivo_agente": sin_ag,
            "sin_motivo_otro": sin_otro, "pct": round(100 * con / ev) if ev else 0}


def motivo_cobertura(cur, account: str, **filters) -> dict:
    """Sobre cuantas sesiones promedia la tarjeta de motivo, y por que las demas quedan afuera."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_MOTIVO_COBERTURA_SQL.format(where=where), params)
    return _build_motivo_cobertura(cur.fetchone())


# DESGLOSE DE "SIN EVALUAR" POR CAUSA. El KPI de arriba dice CUANTAS no se evaluaron; esta
# tarjeta dice POR QUE. Es el mismo arreglo que ya se le hizo al promedio por motivo (ver
# _MOTIVO_STATS_SQL): "era todo lo salteado en una bolsa con el nombre de una sola de sus
# causas". Sin el desglose, las causas no son comparables entre si -- 313 sesiones donde el
# NEGOCIO nunca contesto y 228 donde el cliente solo mando una imagen se leen igual, y son
# problemas de dueño distinto.
# Lo pidio el negocio el 2026-08-13 despues de encontrar que `redireccion` no se veia: la fila
# estaba, pero para contar cuantas eran habia que filtrar la lista y contar a ojo.
# LA POBLACION ES LA MISMA QUE LA DEL KPI (`eval_status <> 'evaluated'`) a proposito: si la
# tarjeta sumara distinto que el numero que tiene arriba, el lector no sabria a cual creerle.
# LA COLUMNA `jugador` ES LA ALERTA. Pedida por el negocio el 2026-08-13: "si son internos
# esta bien, pero si es de canal de jugador si quiero que haya una alerta ahi".
# MEDIDO: de las 313 sesiones `no_agent_reply`, **160 son GRUPOS de WhatsApp** (segmento
# `interno`, numero de 18 digitos tipo `120363433857149469`, 129 mensajes de media de gente
# charlando entre si) y ahi nadie del negocio tiene que contestar. Pero **102 son del segmento
# `jugador`**: 50 en `Jugadores 🍀`, 32 en `OnlySorti`, 13 en `ModoSorti`, 7 en `sortiGO`, con
# 1 o 2 mensajes, CERO grupos y 7 a 12 dias de antiguedad. Personas que escribieron y nadie
# les contesto nunca, escondidas en el mismo renglon que los grupos.
# SE CUELGA DE `jugador` Y NO DE "no es interno" a proposito: `segment_for_queue` devuelve
# 'interno' cuando la cola es NULL o vacia (src/segments.py:51-52), asi que 'interno' no es
# una clasificacion positiva sino "sin cola". `jugador` SI se afirma por nombre de cola.
# Y se cuenta por SEGMENTO, no por cuenta: `jugador` vive en las dos (50 en `sistemas` + 52 en
# `datos`), y partirlo por cuenta haria que ninguna mitad se viera grave.
_SKIP_STATS_SQL = """
SELECT cs.skip_reason AS skip_reason,
       count(*) AS n,
       count(*) FILTER (WHERE cs.segment = 'jugador') AS jugador""" + _SCORES_JOINS + """
   AND cs.eval_status <> 'evaluated'
 GROUP BY 1"""


def _build_skip_stats(rows) -> list[dict]:
    """[{skip_reason, n, pct, jugador}] ordenado por volumen. `pct` sobre el TOTAL SALTEADO.

    La pregunta que contesta la tarjeta es "de lo que no se evaluo, cuanto es cada cosa",
    asi que el denominador es lo salteado y no la poblacion entera.

    `jugador` es el subconjunto que el negocio quiere ver aparte: una sesion salteada de un
    grupo interno no le debe nada a nadie, y una de un jugador que escribio y no recibio
    respuesta es una persona esperando.

    Una fila `skipped` sin `skip_reason` seria un bug del worker, y se muestra como
    `sin_causa` en vez de descartarse: desaparecerla lo esconderia Y romperia el cierre
    contra el KPI, que es lo unico que avisaria del problema.
    """
    total = sum(int(n) for _, n, _ in rows)
    if not total:
        return []
    out = []
    for r, n, jug in rows:
        n, jug = int(n), int(jug or 0)
        if jug > n:
            # El subconjunto no puede ser mayor que el conjunto. Si pasa, alguien toco el
            # FILTER y la alerta estaria hablando de una poblacion que no existe.
            raise ValueError(f"{r!r}: {jug} de jugador sobre {n} sesiones")
        out.append({"skip_reason": r or "sin_causa", "n": n,
                    "pct": round(100 * n / total, 1), "jugador": jug})
    out.sort(key=lambda x: -x["n"])
    return out


def skip_stats(cur, account: str, **filters) -> list[dict]:
    """Por que quedaron sin evaluar las sesiones salteadas (respeta filtros)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_SKIP_STATS_SQL.format(where=where), params)
    return _build_skip_stats(cur.fetchall())


# LAS SITUACIONES QUE DEJARON DE SER SKIP. El 2026-08-21 `no_agent_reply` y `sin_motivo`
# pasaron de saltearse a llevar nota (src/sin_respuesta.py, src/solo_cortesia.py). El chip POR
# FILA se migro, pero el AGREGADO se murio en silencio: la tarjeta del front buscaba
# `skip_reason = 'no_agent_reply'` y el codigo dejo de emitirlo -> la alerta que el negocio
# habia pedido el 2026-08-13 ("si es de canal de jugador si quiero que haya una alerta ahi")
# no se podia mostrar nunca mas. Esta consulta la devuelve, ahora sobre filas EVALUADAS.
#
# POR QUE NO ALCANZA CON EL PROMEDIO DE ESTRELLAS. Un 1 estrella no dice QUE paso; estas dos
# situaciones son de dueño distinto y se leen distinto: "nadie le contesto" es una falla
# nuestra con una persona esperando, y "el cliente no planteo nada" es un cierre que se juzga
# por el estandar y casi siempre esta bien (98,3% medido).
#
# LA COLUMNA `jugador` ES LA ALERTA, por la misma razon que en `_SKIP_STATS_SQL`:
# `segment_for_queue` devuelve 'interno' cuando la cola es NULL o vacia, asi que 'interno' no
# afirma nada -- `jugador` SI se afirma por nombre de cola. Y desde el 2026-08-24 los GRUPOS
# ya no llegan aca: se saltean antes (`grupo_de_whatsapp`), que es lo que separaba las 160
# sesiones de grupo de las 102 de jugador que vivian en el mismo renglon.
_SITUACION_FLAGS = ("sin_respuesta_del_negocio", "solo_cortesia")

_SITUACION_STATS_SQL = """
SELECT CASE
         WHEN (cs.dimensions->>'sin_respuesta_del_negocio')::bool
           THEN 'sin_respuesta_del_negocio'
         WHEN (cs.dimensions->>'solo_cortesia')::bool THEN 'solo_cortesia'
       END AS situacion,
       count(*) AS n,
       count(*) FILTER (WHERE cs.segment = 'jugador') AS jugador,
       avg(cs.stars) AS estrellas""" + _SCORES_JOINS + """
   AND cs.eval_status = 'evaluated'
   AND (coalesce((cs.dimensions->>'sin_respuesta_del_negocio')::bool, false)
        OR coalesce((cs.dimensions->>'solo_cortesia')::bool, false))
 GROUP BY 1"""


def _build_situacion_stats(rows) -> list[dict]:
    """[{situacion, n, pct, jugador, estrellas}] ordenado por volumen.

    `pct` es sobre el total de ESTAS situaciones y no sobre la poblacion evaluada: la
    pregunta de la tarjeta es "de lo que antes era un skip, cuanto es cada cosa" -- mismo
    criterio que `_build_skip_stats`.

    El guard de `jugador > n` es el mismo de skip_stats y por lo mismo: si alguien toca el
    FILTER, la alerta hablaria de una poblacion que no existe. Reventar es mejor que mostrar
    un numero imposible.
    """
    total = sum(int(n) for _, n, _, _ in rows)
    if not total:
        return []
    out = []
    for situacion, n, jug, estrellas in rows:
        n, jug = int(n), int(jug or 0)
        if jug > n:
            raise ValueError(f"{situacion!r}: {jug} de jugador sobre {n} sesiones")
        out.append({"situacion": situacion, "n": n,
                    "pct": round(100 * n / total, 1), "jugador": jug,
                    "estrellas": round(float(estrellas), 2) if estrellas is not None
                    else None})
    out.sort(key=lambda x: -x["n"])
    return out


def situacion_stats(cur, account: str, **filters) -> list[dict]:
    """Las situaciones que antes eran un skip y hoy llevan nota (respeta filtros)."""
    where, params = _scores_filters(account, **filters)
    cur.execute(_SITUACION_STATS_SQL.format(where=where), params)
    return _build_situacion_stats(cur.fetchall())


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
        "motivo_cobertura": motivo_cobertura(cur, account, **filters),
        "skip_stats": skip_stats(cur, account, **filters),
        "situacion_stats": situacion_stats(cur, account, **filters),
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
       cs.atencion, cs.motivo,
       -- El reloj de la primera respuesta viaja en la LISTA (no solo en el detalle):
       -- es el eje de seis de las siete rubricas, y sin el hay que abrir sesion por
       -- sesion para encontrar las lentas. Es un numero, pesa nada.
       cs.first_response_seconds,
       -- El cliente dejó de contestar tras un pedido del operador. Viaja a la LISTA (no
       -- solo al detalle) porque pasa en el 24,7 por ciento de las sesiones (medido el
       -- 2026-08-07; sin el signo a proposito, ver el guard de porcentajes sueltos) y
       -- es el dato que explica que un trámite abierto NO sea culpa del operador. Sin esto
       -- hay que abrir sesión por sesión para entenderlo. Booleano derivado del jsonb: sin
       -- migración y sin peso en el payload.
       (cs.dimensions->>'cliente_abandono')::boolean AS cliente_abandono,
       -- QUE PASO CON EL CLIENTE: se_fue | no_lo_abrio | no_le_llego | dijo_no | null.
       -- El booleano de arriba solo marca `se_fue`; los otros tres finales eran invisibles
       -- y son 48 de 71 en la medicion del 2026-08-12. Ver signals.desenlace_del_cliente.
       cs.dimensions->>'cliente_desenlace' AS cliente_desenlace,
       """ + OPERADOR_O_NADA + """ AS user_name, cs.user_id
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


def filter_options(cur, account: str, ambiente: str = "todos") -> dict:
    """Valores para los desplegables de filtros (segmento, canal, operador, motivo).

    RECORTADOS POR AMBIENTE desde el 2026-08-07. Antes salian de la cuenta entera y en
    `agente` el desplegable de motivo ofrecia los 7, aunque ahi las sesiones se califican
    con agilidad y el motivo es NULL: elegir "Depósito" devolvia CERO filas sin ninguna
    explicacion. Un desplegable no puede prometer un filtro que el ambiente no puede dar.
    Estable por cuenta+ambiente -> el front lo pide una vez por combinacion."""
    from src.segments import segments_for_ambiente

    params: dict = {"account": account}
    amb = ""
    if ambiente and ambiente != "todos":
        amb = " AND cs.segment = ANY(%(amb_segments)s)"
        params["amb_segments"] = list(segments_for_ambiente(ambiente))
    # Alias `cs` en las cuatro para que el predicado del ambiente sea el MISMO texto.
    cur.execute("SELECT DISTINCT cs.segment FROM conversation_scores cs "
                "WHERE cs.account = %(account)s AND cs.segment IS NOT NULL" + amb
                + " ORDER BY 1", params)
    segments = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT t.channel FROM conversation_scores cs "
                "JOIN tickets t ON t.id = cs.ticket_id "
                "WHERE cs.account = %(account)s AND t.channel IS NOT NULL" + amb
                + " ORDER BY 1", params)
    channels = [r[0] for r in cur.fetchall()]
    # Mismo criterio que _OPS_SQL: el desplegable ofrece lo que los cuadros muestran. Si
    # divergen, el filtro no puede llegar a filas que igual estan en el promedio.
    cur.execute(f"SELECT DISTINCT {_OPERADOR_RESUELTO} AS op FROM conversation_scores cs "
                "LEFT JOIN users u ON u.id = cs.user_id WHERE cs.account = %(account)s "
                f"AND {HAY_OPERADOR}"
                + amb + " ORDER BY 1", params)
    operators = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT cs.motivo FROM conversation_scores cs "
                "WHERE cs.account = %(account)s AND cs.motivo IS NOT NULL" + amb
                + " ORDER BY 1", params)
    motivos = [r[0] for r in cur.fetchall()]
    return {"segments": segments, "channels": channels, "operators": operators,
            "motivos": motivos}


def _juzgada_desde(dimensions):
    """El arranque de la interaccion juzgada, si la fila lo trae. Tolerante a proposito: las
    filas de antes de v9 no lo tienen y una fecha ilegible no puede tumbar el modal."""
    from datetime import datetime
    crudo = (dimensions or {}).get("interaccion_juzgada_desde") if isinstance(dimensions, dict) else None
    if not crudo:
        return None
    try:
        return datetime.fromisoformat(crudo)
    except (TypeError, ValueError):
        return None


def _transcript(msgs: list[dict], juzgada_desde=None) -> list[dict]:
    """El chat del modal, con cada mensaje ubicado en SU interaccion.

    Una sesion mergea todos los episodios del ticket y en el 10,2% de las de `jugador` son
    VARIAS atenciones seguidas -- medido el 2026-08-12: una sesion con 17, y saltos de 51
    horas entre una y la siguiente. El modal las mostraba como un solo chat corrido, asi que
    quien auditaba leia una nota de 2 estrellas al lado de un tramo que habia salido bien y
    concluia que el sistema se equivocaba. La nota describe UNA interaccion, no la sesion.

    `juzgada_desde` = el arranque de la interaccion que la rubrica miro. No hace falta
    guardarlo: el worker ya sobreescribe `conversation_created_at` de la fila con ese
    instante cuando hay ancla determinista (deposito/retiro/registro). Sin ancla -- el
    fall-through, donde el LLM lee la sesion COMPLETA -- no se marca ninguna en particular:
    todas quedan juzgadas, porque elegir una seria decidir por el negocio cual representa la
    nota. La frontera es la de `src/interacciones.py`, la MISMA que usa el scoring; si el
    front dibujara la suya, el corte que se ve y el que se califica podrian no coincidir.
    """
    from src.interacciones import partir_en_interacciones
    idx_de = {}
    total = 0
    juzgadas = set()
    if all(m.get("created_at") is not None for m in msgs):
        for n, interaccion in enumerate(partir_en_interacciones(msgs), 1):
            total = n
            arranca = min(m["created_at"] for m in interaccion)
            termina = max(m["created_at"] for m in interaccion)
            if juzgada_desde is None or arranca <= juzgada_desde <= termina:
                juzgadas.add(n)
            for m in interaccion:
                idx_de[id(m)] = n
    out = []
    for m in msgs:
        # OPERADOR = nuestro personal de soporte. NO "AGENTE": el agente es el CLIENTE
        # vendedor/afiliador (segmento `agente`, cola "Agente 👨👩"), del otro lado del chat.
        # BOT NO ES SOLO `CHATBOT`. El rotulo miraba un unico remitente, asi que el marketing
        # masivo por `api` ("*¡Aficionados al fútbol, la emoción está por comenzar!*")
        # aparecia en el chat como OPERADOR -- indistinguible de una persona. Son 1.167
        # mensajes. `sin_persona_detras` decide por REMITENTE, nunca por la falta de user_id:
        # 230.773 mensajes de operadores reales vienen sin `sent_from` (ver src/metrics.py).
        from src.metrics import sin_persona_detras
        from src.censura import censurar_texto

        # LAS NOTAS DEL CRM VAN COMO `SISTEMA`, ni como operador ni escondidas.
        # Se filtraban porque el chat era "lo que se le dijo al cliente", y eso deja de
        # alcanzar en cuanto los NUMEROS de la tarjeta se anclan en ellas. CASO REAL
        # (2026-08-24, `e97b75aa`): "tardo 1,7 minutos en avisar que recibio el comprobante"
        # sobre un chat con dos horas visibles separadas por DOS minutos. El 1,7 se mide
        # desde `14:01:04 Anggie *aceptado*` -- la nota de ENTREGA del ticket, que es donde
        # `inicio_del_reloj` arranca (src/deposito.py) -- hasta el "ing" de 14:02:46. Con la
        # nota escondida ese numero no se puede verificar mirando el chat.
        # `SISTEMA` y no `OPERADOR` a proposito: contarla como respuesta al cliente seria el
        # bug que `hubo_respuesta_del_negocio` ya documenta en src/sin_respuesta.py.
        if m.get("is_note"):
            role = "SISTEMA"
        else:
            role = ("CLIENTE" if not m["from_me"]
                    else ("BOT" if sin_persona_detras(m) else "OPERADOR"))
        # La HORA de cada mensaje viaja al front. Es lo que permite ver la demora de un
        # vistazo en el chat, que es donde se entiende: un salto de 40 minutos entre el
        # pedido y la respuesta no se lee en ningun KPI. Va en ISO y el front la formatea en
        # hora de Ecuador (la operacion corre 06:00-23:59 alla).
        at = m.get("created_at")
        n = idx_de.get(id(m), 1)
        # EL DATO SENSIBLE SE ENMASCARA ACA, en el camino de LECTURA. El tablero vive en
        # un dominio publico con 14 endpoints anonimos que devuelven este transcript
        # completo (auditoria del 2026-08-24), y MEDIDO sobre los 52.135 mensajes de las
        # sesiones scoreadas: 3.734 traen un celular, 2.186 una corrida de 6+ digitos, 685
        # una cuenta bancaria y **399 usuario y clave EN CLARO**, 384 de operadores.
        # NO se censura antes de calificar: el scoring lee los mensajes crudos por
        # `context.fetch_session_messages`, y `es_traspaso` compara el tail del telefono
        # contra el mapa de lineas, `_es_traspaso_de_datos` busca el email o la cedula y
        # `operator_sent_credentials` busca justo el patron de credenciales. Hay un test
        # que prohibe el import de `censura` en los modulos de scoring.
        # ANTES DEL TRUNCADO a 800: el enmascarado conserva el largo, asi que el corte cae
        # en el mismo lugar; al reves, un telefono partido por el corte no matchearia y
        # saldria a medias en claro.
        out.append({"role": role,
                    "text": censurar_texto((m.get("body") or "[media]").strip())[:800],
                    "at": at.isoformat() if at is not None else None,
                    "interaccion": n, "interacciones": total or 1,
                    "juzgada": n in juzgadas if juzgadas else True})
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


# Baja lógica en los cuadros FULL-SCALE de /api/charts. Estos no pasan por
# `_scores_filters` (van sobre `conversations`, no sobre `conversation_scores`), así que
# necesitan su propia cláusula: sin esto un operador apagado desaparecía de todo el
# dashboard MENOS de estos dos, el peor de los dos mundos.
#
# EL NOMBRE DEL OPERADOR EN LOS CUADROS. Acá no existe `conversation_scores.user_name`
# (se lee `conversations`/`messages`), así que la firma '*Nombre:*' se reconstruye en SQL
# con el CTE `op_sig` — la misma fuente que usa src/operators.build_operator_map para el
# scoring. El fallback tiene que ser el MISMO string que guarda `operator_status`, o el
# apagado no matchearía nunca.
#
# POR QUE. Medido el 2026-08-07 en `sistemas`: 29 operadores existen en `users` (729.683
# mensajes) y **38 NO existen** (502.766 mensajes, el 40,8%). Resolviendo solo por
# `users.name`, esas 38 personas colapsaban en UNA fila 'Operador sin identificar': un
# operador ficticio gigante en la carga y un promedio sobre 38 personas en el % de
# depósito. La firma rescata 34 de los 38; 4 no firman nunca y siguen en el fallback.
#
# Y ARREGLA UN AGUJERO MÁS GRAVE que el nombre: el modal apaga por el nombre REAL (que sí
# resuelve, vía `cs.user_name`), pero acá se comparaba contra 'Operador sin identificar'
# -> para esos 38 la BAJA LÓGICA no funcionaba en los cuadros. Apagabas a alguien, seguía
# apareciendo, y no había forma de saber por qué.
# La firma llega por el CTE `op_sig`, no por `cs`: aca no hay `conversation_scores`.
_OP_CHARTS = expr_resuelto(firma="sig.name", user_id="co.user_id")

# user_id -> nombre firmado más frecuente. Espeja build_operator_map (nombre más frecuente
# por operador, no el último). El tiebreaker por nombre lo hace determinista: sin él, dos
# firmas con el mismo conteo alternan entre corridas y el cuadro cambia sin que cambien
# los datos.
#
# LLAVES DOBLADAS a propósito: este CTE se embebe en `_LOAD_SQL`/`_DEP_PCT_SQL`, que pasan
# por `.format(cola=..., apagados=...)`, y ahí `{2,40}` del cuantificador del regex se
# interpreta como un placeholder (`KeyError: '2,40'`). `{{2,40}}` llega a Postgres como
# `{2,40}`. Si algún día este CTE se usa en una query que NO se formatea, hay que
# des-doblarlas.
# El nombre del operador llega YA RESUELTO desde Python (src/operators.build_operator_map),
# no se re-deriva en SQL. Dos razones:
#  - CORRECCION: la canonicalizacion por PERSONA — unificar los user_id que el CRM recreo,
#    sacando tildes y eligiendo la grafia dominante — no se puede hacer en SQL plano. Sin
#    ella, alguien con dos cuentas aparece como dos operadores y apagarlo en la
#    configuracion no lo apaga entero. Medido el 2026-08-07: 10 personas, 362.944 mensajes.
#  - COSTO: el CTE con regex sobre `messages` se calculaba en las TRES queries de cada
#    request de /api/charts.
# MATERIALIZED igual: sin eso el planner estimaba `conv_op` en 200 filas (son ~17.000),
# elegia Nested Loops en cascada y en la cuenta `datos` la query pasaba de 0,2s a mas de 90s
# -> /api/charts devolvia 500 al cortar en el statement_timeout.
_OP_SIG_CTE = """op_sig AS MATERIALIZED (
  SELECT * FROM unnest(%(sig_ids)s::uuid[], %(sig_names)s::text[]) AS t(user_id, name)
)"""
def _sig_params(op_map: dict | None) -> dict:
    """Los dos arrays paralelos del mapa de identidad. Vacios = sin mapa (cae al nombre de
    `users` y al fallback), que es la conducta de antes."""
    m = op_map or {}
    return {"sig_ids": list(m.keys()), "sig_names": list(m.values())}


_SIN_APAGADOS_CHARTS = f"""
   AND NOT EXISTS (
     SELECT 1 FROM operator_status os
      WHERE os.account = %(account)s AND os.activo = false
        AND {_clave_sql('os.operator_name')} = {_clave_sql(_OP_CHARTS)})"""


# --- §10: carga mensual por operador (segmento jugador). Operador = el user_id
# con más mensajes de negocio en la conversación (conversations.user_id suele ser
# NULL). Se acota a las colas jugador y se agrupa por (mes, operador).
_LOAD_SQL = """
WITH msg_op AS MATERIALIZED (
  SELECT conversation_id, user_id, count(*) AS n
    FROM messages
   WHERE account = %(account)s AND from_me AND NOT is_note AND user_id IS NOT NULL
   GROUP BY conversation_id, user_id
),
conv_op AS MATERIALIZED (
  SELECT DISTINCT ON (conversation_id) conversation_id, user_id
    FROM msg_op ORDER BY conversation_id, n DESC
),
""" + _OP_SIG_CTE + """
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       """ + _OP_CHARTS + """ AS op,
       count(*) AS conv
  FROM conversations c
  JOIN conv_op co ON co.conversation_id = c.id
  LEFT JOIN users u ON u.id = co.user_id
  LEFT JOIN op_sig sig ON sig.user_id = co.user_id
 WHERE c.account = %(account)s AND c.created_at IS NOT NULL AND {cola}""" \
    + _MONTH_WINDOW + "{apagados}" + """
 GROUP BY 1, 2
"""


def _queue_ids_for_ambiente(cur, account: str, ambiente: str = "jugador") -> list:
    """IDs de las colas que componen un AMBIENTE (clasificadas con segment_for_queue).

    OJO: la lista de colas NO alcanza para describir un ambiente que incluya la cola
    vacia. En la BD no existe ninguna cola de nombre vacio: las conversaciones de
    "cola vacia" tienen `queue_id IS NULL` (10.939 medidas el 2026-08-07), asi que por
    lista son inalcanzables. Quien filtre por estos ids debe combinarlos con
    `_cola_pred(ambiente_incluye_sin_cola(ambiente))`, no usarlos sueltos.
    """
    from src.segments import segment_for_queue, segments_for_ambiente

    segs = set(segments_for_ambiente(ambiente))
    cur.execute("SELECT id, name FROM queues WHERE account = %s", (account,))
    return [qid for qid, name in cur.fetchall() if segment_for_queue(name) in segs]


def _jugador_queue_ids(cur, account: str) -> list:
    """IDs de las colas del segmento jugador.

    Sigue existiendo porque `src/conversions.py` precomputa `player_conversions`, que es
    la conversion de jugador potencial a jugador: ahi el recorte a jugador es la
    DEFINICION de la metrica, no un filtro de tablero que deba seguir al switch.
    """
    return _queue_ids_for_ambiente(cur, account, "jugador")


# Etiqueta de las conversaciones sin cola asignada (`queue_id IS NULL`). El nombre importa:
# el codigo las clasifica como 'interno' pero NO son personal hablando entre si — medido el
# 2026-08-07, el 90% tiene mensajes de cliente reales y arrastran 6.795 comprobantes. Decir
# "sin cola asignada" es lo que efectivamente sabemos; decir "interno" seria afirmar de mas.
SIN_COLA_LABEL = "(sin cola asignada)"

# Cuenta CONVERSACIONES, no sesiones, por dos razones: es el grano que los cuadros de
# /api/charts filtran por `queue_id`, y la version que pasaba por conversation_sessions
# (JOIN de 128k sesiones contra conversations) se comia el statement_timeout de 20s del
# endpoint. Esta corre en 24 ms medidos.
_COMPOSICION_SQL = """
SELECT coalesce(q.name, '') AS cola, count(*) AS conversaciones
  FROM conversations c
  LEFT JOIN queues q ON q.id = c.queue_id
 WHERE c.account = %(account)s
 GROUP BY 1
"""


def ambiente_composition(cur, account: str) -> dict:
    """Que compone cada ambiente en ESTA cuenta: colas, segmentos y conversaciones.

    Existe para que el tablero pueda DECIR el origen de lo que muestra en vez de que el que
    mira lo deduzca. Devuelve tambien 'todos' (la suma), asi el front puede mostrar el peso
    relativo de cada ambiente sin recalcular nada.
    """
    from src.segments import ambiente_for_segment, segment_for_queue, segments_for_ambiente

    cur.execute(_COMPOSICION_SQL, {"account": account})
    acc: dict = {amb: {"conversaciones": 0, "segmentos": set(), "colas": []}
                 for amb in ("todos", "jugador", "agente", "sin_clasificar")}
    for cola, conversaciones in cur.fetchall():
        segmento = segment_for_queue(cola)
        ambiente = ambiente_for_segment(segmento)
        etiqueta = cola or SIN_COLA_LABEL
        n = int(conversaciones)
        for destino in (ambiente, "todos"):
            acc[destino]["conversaciones"] += n
            acc[destino]["segmentos"].add(segmento)
            acc[destino]["colas"].append({"cola": etiqueta, "segmento": segmento,
                                          "conversaciones": n})
    return {
        "account": account,
        "ambientes": {
            amb: {
                "conversaciones": d["conversaciones"],
                # orden estable: el ambiente define el orden, no el azar del set
                "segmentos": [s for s in segments_for_ambiente(amb) if s in d["segmentos"]],
                "colas": sorted(d["colas"], key=lambda c: -c["conversaciones"]),
            }
            for amb, d in acc.items()
        },
    }


def _cola_pred(incluye_sin_cola: bool) -> str:
    """Predicado de cola para los cuadros full-scale (que filtran sobre conversations).

    Con la cola vacia adentro hay que sumar `OR c.queue_id IS NULL`: sin eso el ambiente
    `sin_clasificar` sale vacio y `todos` pierde 10.939 conversaciones en silencio.
    """
    if incluye_sin_cola:
        return "(c.queue_id = ANY(%(qids)s) OR c.queue_id IS NULL)"
    return "c.queue_id = ANY(%(qids)s)"


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
                     window_months: int = DEFAULT_WINDOW_MONTHS,
                     inactivos: str = "ocultar", ambiente: str = "jugador", op_map: dict | None = None) -> dict:
    """Carga mensual por operador del AMBIENTE, top-N + 'Otros', últimos N meses.

    Los operadores apagados no aparecen (baja lógica). Con el filtro puesto, 'Otros' pasa a
    ser "el resto de los ACTIVOS", que es lo que corresponde.

    `ambiente` default 'jugador' = la conducta histórica de este cuadro."""
    from src.segments import ambiente_incluye_sin_cola

    qids = _queue_ids_for_ambiente(cur, account, ambiente)
    sin_cola = ambiente_incluye_sin_cola(ambiente)
    if not qids and not sin_cola:
        return {"months": [], "series": []}
    sql = _LOAD_SQL.format(apagados="" if inactivos == "incluir" else _SIN_APAGADOS_CHARTS,
                           cola=_cola_pred(sin_cola))
    cur.execute(sql, {"account": account, "qids": qids, "months_back": window_months - 1,
                      **_sig_params(op_map)})
    return _build_load_series(cur.fetchall(), top_n)


# --- §2: % depósito en WhatsApp por operador (jugador). Une operador dominante +
# flag de depósito por conversación, acotado a WhatsApp y colas jugador.
_DEP_PCT_SQL = """
WITH msg_op AS MATERIALIZED (
  SELECT conversation_id, user_id, count(*) AS n
    FROM messages
   WHERE account = %(account)s AND from_me AND NOT is_note AND user_id IS NOT NULL
   GROUP BY conversation_id, user_id
),
conv_op AS MATERIALIZED (
  SELECT DISTINCT ON (conversation_id) conversation_id, user_id
    FROM msg_op ORDER BY conversation_id, n DESC
),
conv_dep AS MATERIALIZED (
  SELECT conversation_id,
         -- from_me = false: el contexto de recarga lo pone el CLIENTE. La
         -- plantilla de venta del operador lo menciona en casi toda
         -- prospeccion e inflaba el gate un 41,4 por ciento (medido el 2026-08-06).
         -- Mismo criterio que src.deposits.has_recharge_context.
         bool_or((body ~* %(re)s) AND NOT is_note AND from_me = false) AS has_ctx,
         count(*) FILTER (WHERE from_me = false AND NOT is_note
                          AND lower(coalesce(media_type, '')) LIKE '%%image%%') AS img
    FROM messages WHERE account = %(account)s GROUP BY conversation_id
),
""" + _OP_SIG_CTE + """
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       """ + _OP_CHARTS + """ AS op,
       count(*) AS conv,
       count(*) FILTER (WHERE cd.has_ctx AND cd.img > 0) AS con_dep
  FROM conversations c
  JOIN conv_op co ON co.conversation_id = c.id
  LEFT JOIN conv_dep cd ON cd.conversation_id = c.id
  LEFT JOIN users u ON u.id = co.user_id
  LEFT JOIN op_sig sig ON sig.user_id = co.user_id
  JOIN tickets t ON t.id = c.ticket_id
 WHERE c.account = %(account)s AND {cola}
   AND t.channel = 'WHATSAPP' AND c.created_at IS NOT NULL""" \
    + _MONTH_WINDOW + "{apagados}" + """
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
                            window_months: int = DEFAULT_WINDOW_MONTHS,
                            inactivos: str = "ocultar", ambiente: str = "jugador", op_map: dict | None = None) -> dict:
    """§2: % depósito en WhatsApp por operador del AMBIENTE, top-N + 'Otros', últimos N meses.
    Los operadores apagados no aparecen (baja lógica).

    Este cuadro es el que más gana con el switch: medido el 2026-08-07, los agentes carrean
    121.180 de los 168.919 comprobantes (71,7%) contra 40.807 de jugador (24,2%). Clavado en
    jugador, mostraba el cuarto de la evidencia de depósito."""
    from src.deposits import RECHARGE_PATTERN
    from src.segments import ambiente_incluye_sin_cola

    qids = _queue_ids_for_ambiente(cur, account, ambiente)
    sin_cola = ambiente_incluye_sin_cola(ambiente)
    if not qids and not sin_cola:
        return {"months": [], "series": []}
    sql = _DEP_PCT_SQL.format(apagados="" if inactivos == "incluir" else _SIN_APAGADOS_CHARTS,
                              cola=_cola_pred(sin_cola))
    cur.execute(sql, {"account": account, "re": RECHARGE_PATTERN, "qids": qids,
                      "months_back": window_months - 1, **_sig_params(op_map)})
    return _build_pct_series(cur.fetchall(), top_n, min_conv)


# --- §9: nuevos jugadores vs % depósito por mes (jugador, agregado). Dos medidas
# de escala distinta -> el front las muestra en DOS paneles (no doble-eje).
_NEW_VS_DEP_SQL = """
WITH per_conv AS MATERIALIZED (
  SELECT conversation_id,
         -- from_me = false: el contexto de recarga lo pone el CLIENTE. La
         -- plantilla de venta del operador lo menciona en casi toda
         -- prospeccion e inflaba el gate un 41,4 por ciento (medido el 2026-08-06).
         -- Mismo criterio que src.deposits.has_recharge_context.
         bool_or((body ~* %(re)s) AND NOT is_note AND from_me = false) AS has_ctx,
         count(*) FILTER (WHERE from_me = false AND NOT is_note
                          AND lower(coalesce(media_type, '')) LIKE '%%image%%') AS img
    FROM messages WHERE account = %(account)s GROUP BY conversation_id
)
SELECT to_char(c.created_at, 'YYYY-MM') AS mes,
       count(*) AS conv,
       count(*) FILTER (WHERE pc.has_ctx AND pc.img > 0) AS con_dep,
       count(*) FILTER (WHERE c.is_new_contact) AS nuevos,
       -- misma población que las barras del cuadro: de los NUEVOS del mes, cuántos
       -- trajeron comprobante. Es el numerador que le faltaba a la línea.
       count(*) FILTER (WHERE c.is_new_contact AND pc.has_ctx AND pc.img > 0) AS nuevos_con_dep
  FROM conversations c
  LEFT JOIN per_conv pc ON pc.conversation_id = c.id
 WHERE c.account = %(account)s AND {cola} AND c.created_at IS NOT NULL""" + _MONTH_WINDOW + """
 GROUP BY 1
"""


def _build_new_vs_deposit(rows) -> dict:
    """{months, nuevos[], pct[], pct_nuevos[]} desde filas
    (mes, conv, con_dep, nuevos, nuevos_con_dep). Puro.

    DOS denominadores distintos, a propósito y ahora explícitos:
    - `pct`        = con_dep / conv       -> % de TODAS las conversaciones del segmento
                                             jugador que tuvieron comprobante (métrica §9).
    - `pct_nuevos` = nuevos_con_dep / nuevos -> % de los jugadores NUEVOS del mes que
                                             depositaron. ESTA es la que comparte población
                                             con las barras del cuadro.
    Antes solo existía `pct` y la leyenda decía "línea = % que depositó" al lado de barras
    de jugadores nuevos, así que se leía como si fuera `pct_nuevos`. En julio de `sistemas`
    eran 60,4% contra 33,8%: casi el doble.

    `pct_nuevos` es None (no 0.0) cuando el mes no tuvo jugadores nuevos: "no hubo nuevos"
    no es lo mismo que "ninguno depositó", y el 0 dibujaría una caída inexistente.
    """
    rows = sorted(rows, key=lambda r: r[0])
    months = [r[0] for r in rows]
    nuevos = [int(r[3]) for r in rows]
    pct = [round(100.0 * int(r[2]) / int(r[1]), 1) if int(r[1]) else 0.0 for r in rows]
    pct_nuevos = [
        round(100.0 * int(r[4]) / int(r[3]), 1) if int(r[3]) else None for r in rows
    ]
    return {"months": months, "nuevos": nuevos, "pct": pct, "pct_nuevos": pct_nuevos}


def new_vs_deposit_by_month(cur, account: str,
                            window_months: int = DEFAULT_WINDOW_MONTHS,
                            ambiente: str = "jugador", op_map: dict | None = None) -> dict:
    """§9: contactos nuevos y % depósito por mes del AMBIENTE, últimos N meses.

    `is_new_contact` cuenta la PRIMERA vez que aparece una persona, así que en `agente` lo
    que cuenta son agentes nuevos, no jugadores. Es la misma métrica sobre otra audiencia:
    quién la mira lo sabe por la composición que devuelve /api/charts."""
    from src.deposits import RECHARGE_PATTERN
    from src.segments import ambiente_incluye_sin_cola

    qids = _queue_ids_for_ambiente(cur, account, ambiente)
    sin_cola = ambiente_incluye_sin_cola(ambiente)
    if not qids and not sin_cola:
        return {"months": [], "nuevos": [], "pct": []}
    sql = _NEW_VS_DEP_SQL.format(cola=_cola_pred(sin_cola))
    cur.execute(sql, {"account": account, "re": RECHARGE_PATTERN, "qids": qids,
                      "months_back": window_months - 1, **_sig_params(op_map)})
    return _build_new_vs_deposit(cur.fetchall())


# =====================================================================
# Conversión jugador potencial -> jugador. Agrega la tabla player_conversions
# (precomputada por el pase determinista de src/conversions.py; 1 fila/persona).
# Filtrable por canal/segmento/operador/fecha de ENTRADA (first_at = cohorte por
# mes; la conversión es first-touch). NO usa estado/rating (no aplican al potencial).
# Operador = user_id (entidad users); NULL = bot/sin asignar.
# =====================================================================
# `potential_clients` no guarda firma: si hay `user_id` y no hay fila en `users`, es un
# usuario que el CRM borro, y esa es la etiqueta que corresponde (no "sin identificar").
_CONV_OP_EXPR = ("CASE WHEN pc.user_id IS NULL THEN 'BOT / sin operador' "
                 f"ELSE {expr_resuelto(firma=None, user_id='pc.user_id')} END")


# Baja lógica en la conversión. Tercera expresión distinta de "operador": acá cuelga de
# `player_conversions.user_id` (el primer agente que tocó al jugador). 'BOT / sin operador'
# no es una persona y nunca está en operator_status, así que jamás se esconde.
_SIN_APAGADOS_CONV = f"""NOT EXISTS (
     SELECT 1 FROM operator_status os
      WHERE os.account = pc.account AND os.activo = false
        AND {_clave_sql('os.operator_name')} = {_clave_sql(_CONV_OP_EXPR)})"""


def _conversion_where(account: str, *, canal="all", segment="all", op="all",
                      date_from=None, date_to=None, inactivos="ocultar",
                      **_ignored) -> tuple[str, dict]:
    """(where, params) sobre player_conversions. Ignora filtros que no aplican al
    potencial (estado/rating/búsqueda). fecha = first_at (mes de entrada)."""
    where = ["pc.account = %(account)s"]
    params: dict = {"account": account}
    if inactivos != "incluir":
        where.append(_SIN_APAGADOS_CONV)
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
       """ + expr_resuelto(firma=None, user_id='pc.user_id') + """ AS op,
       count(*) AS n,
       count(*) FILTER (WHERE pc.deposited) AS conv,
       count(*) FILTER (WHERE pc.attention IS NOT NULL) AS clasif,
       count(*) FILTER (WHERE pc.attention = 'pasivo') AS pasiva
  FROM player_conversions pc
  JOIN users u ON u.id = pc.user_id
 WHERE {where} AND pc.first_at IS NOT NULL AND pc.user_id IS NOT NULL
 GROUP BY 1, 2"""


def _build_conversion_passivity(rows, top_n: int | None = None, min_conv: int = 5) -> dict:
    """{months, operators:[{name, n, conv:[%|None], pasiva:[%|None]}]} para el cuadro
    verde(conv)/rojo(pasiva) por operador. conv% sobre total; pasiva% sobre clasificadas.
    Mes-operador con <min_conv -> None (rompe la línea).

    `top_n=None` (default) = TODOS los operadores, ordenados por volumen desc. Como
    _build_quality_evolution, dibuja UN mini-gráfico POR operador -> sumar operadores no
    degrada nada. El front decide cuántos muestra."""
    by: dict[str, dict] = {}
    for mes, op, n, conv, clasif, pasiva in rows:
        by.setdefault(op, {})[mes] = (int(n), int(conv), int(clasif), int(pasiva))
    months = sorted({m for ms in by.values() for m in ms})
    totals = {op: sum(v[0] for v in ms.values()) for op, ms in by.items()}
    ranked = sorted(totals, key=lambda o: (-totals[o], o))
    if top_n is not None:
        ranked = ranked[:top_n]
    operators = []
    for op in ranked:
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
        operators.append({"name": op, "n": totals[op], "conv": conv_s, "pasiva": pasv_s})
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
    from src.signals import ANYTHING_ELSE_PATTERN

    cur.execute(_DETAIL_SQL, {"cid": conversation_id,
                              "algo_mas_re": ANYTHING_ELSE_PATTERN})
    row = cur.fetchone()
    if row:
        cols = [d.name for d in cur.description]
        d = {c: _coerce(v) for c, v in zip(cols, row)}
        # CUAL interaccion se califico. Sale de `dimensions`, no de `conversation_created_at`:
        # cuando el ancla elige la primera, ese campo queda identico a no tener ancla, y los
        # dos casos piden marcados opuestos (ver src/worker.py). Las filas anteriores a v9 no
        # lo traen -> no se senala ninguna, que es lo honesto: no sabemos cual fue.
        # EL CHAT QUE SE MUESTRA TIENE QUE SER EL QUE SE CALIFICO. La nota se calcula sobre
        # `fetch_session_messages(session_id)` -- todos los episodios de la sesion mergeados
        # (src/worker.py) --, y aca se cargaba `fetch_messages(conversation_id)`, que trae
        # SOLO el episodio de entrada. Como en las filas por sesion `conversation_id ==
        # session_id`, el modal mostraba el PRIMER episodio y la nota describia otro tramo.
        # CASO REAL (2026-08-24): un modal con TRES mensajes donde el operador contesta en 2
        # minutos, al lado de "16 mensajes · 11 cliente / 5 operador", "2 operadores en esta
        # sesion" y un rationale que acusa de tardar 5,8 minutos. La nota no estaba mal; la
        # evidencia mostrada no era la que se juzgo, que para el que mira es peor.
        # Sin `session_id` (filas viejas del path por conversacion) el episodio ES todo lo que
        # hay: se cae al fallback, porque pedir por sesion devolveria vacio.
        sid = d.get("session_id")
        mensajes = (fetch_session_messages(cur, sid) if sid
                    else fetch_messages(cur, conversation_id))
        d["transcript"] = _transcript(mensajes,
                                      juzgada_desde=_juzgada_desde(d.get("dimensions")))
        return d
    transcript = _transcript(fetch_messages(cur, conversation_id))
    if not transcript:
        return None
    return {"conversation_id": conversation_id, "eval_status": None,
            "pending": True, "transcript": transcript}


# --- COMPATIBILIDAD DE LAS TARJETAS CON EL AMBIENTE -------------------------------
# AUDITORIA del 2026-08-07 sobre las 25 queries del tablero: 12 respetan el ambiente via
# `_scores_filters`, 5 via queue_ids, y **4 lo IGNORABAN** — las de conversion. La causa:
# `_conversion_where` recibe `**_ignored` y se tragaba el `ambiente`, y `player_conversions`
# guarda `'jugador'` HARDCODEADO (src/conversions.py:120).
# Verificado en vivo: /api/conversion devolvia el MISMO hash md5 para jugador, agente y
# sin_clasificar. Apretabas "Agentes" y seguias viendo jugadores, sin aviso. Una tarjeta
# vacia se nota; una que muestra otra cosa, no.
#
# NO se "arregla" agregandole el filtro: la conversion es POR DEFINICION "jugador potencial
# -> jugador" (`player_conversions` existe solo para el segmento jugador). No es un filtro
# que falte, es una metrica que solo aplica a una audiencia. Se DECLARA.
def conversion_aplica(ambiente: str = "todos") -> bool:
    """La conversion jugador-potencial -> jugador aplica a este ambiente?

    'todos' aplica porque es la SUMA, no una audiencia: la conversion de los jugadores que
    hay adentro sigue siendo un dato real, y esconderla seria peor que mostrarla.
    """
    return ambiente in ("todos", "jugador")
