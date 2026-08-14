"""Tests de la capa de queries: lo importante es que TODA lectura de scores
esta scopeada por cuenta (datos vs sistemas conviven en la misma BD)."""
from decimal import Decimal

from src.router import ANOMALOUS_MESSAGE_MAX
from src.identidad import OPERADOR_RESUELTO
from src.queries import (
    _build_dep_channel,
    _build_load_series,
    _build_motivo_stats,
    _build_ops_motivo,
    _build_conversion_by_month,
    _build_conversion_passivity,
    _build_conversion_ranking,
    _build_new_vs_deposit,
    _build_ops,
    _build_pct_series,
    _build_quality_evolution,
    _build_quality_motivo,
    _QUALITY_MOTIVO_SQL,
    _conversion_where,
    _DEP_PCT_SQL,
    _DETAIL_SQL,
    _LOAD_SQL,
    _SIN_APAGADOS_CHARTS,
    SIN_COLA_LABEL,
    ambiente_composition,
    deposit_pct_by_operator,
    load_by_operator,
    _dist_from_labels,
    _scores_filters,
    _sort_convs,
    _ticket_cards,
    _TICKETS_CONVS_SQL,
    conversation_detail,
    conversion_by_month,
    conversion_by_operator,
    conversion_cohort,
    conversion_passivity_evolution,
    deposit_by_channel,
    distribution,
    filter_options,
    new_vs_deposit_by_month,
    operators_table,
    pending_sessions_count,
    scored_rows,
    summary,
    summary_kpis,
    tickets_page,
)


class _FakeCursor:
    def __init__(self, rows=(), description=(), one=None):
        self._rows = rows
        self._one = one
        self.description = [type("C", (), {"name": n})() for n in description]
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._one is not None:
            return self._one
        return self._rows[0] if self._rows else None


def test_scored_rows_siempre_filtra_por_cuenta():
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, params = cur.executed[0]
    assert "cs.account = %(account)s" in query
    assert params["account"] == "datos"


def test_scored_rows_devuelve_dicts_por_columna():
    cur = _FakeCursor(
        [("c1", "sistemas", "buena")],
        description=["conversation_id", "account", "rating_label"],
    )
    rows = scored_rows(cur, "sistemas")
    assert rows == [{"conversation_id": "c1", "account": "sistemas", "rating_label": "buena"}]


def test_scored_rows_coacciona_decimal_a_numero():
    # Postgres numeric -> Decimal en psycopg -> si sale como string en el JSON,
    # el front concatena en vez de sumar (bug del 7.19e+46). Se coacciona aca.
    cur = _FakeCursor(
        [("c1", Decimal("5"), Decimal("12.5"))],
        description=["conversation_id", "stars", "resolution_seconds"],
    )
    rows = scored_rows(cur, "datos")
    assert rows[0]["stars"] == 5.0 and isinstance(rows[0]["stars"], float)
    assert rows[0]["resolution_seconds"] == 12.5 and isinstance(rows[0]["resolution_seconds"], float)


def test_scored_rows_resuelve_operador_por_users():
    # Fuente canonica del nombre = tabla `users` (poblada por el monitor del ETL).
    # La firma '*Nombre:*' (cs.user_name) queda solo de fallback.
    # La etiqueta la arma el SQL, no el front: si no hay NINGUN rastro de operador el campo
    # llega NULL (conversacion 100% bot) y si lo hay, llega ya resuelto -- con el split del
    # usuario que borro el CRM incluido.
    from src.identidad import OPERADOR_O_NADA
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    assert "JOIN users" in query
    assert f"{OPERADOR_O_NADA} AS user_name" in query


def test_scored_rows_incluye_contact_id_para_agrupar_por_cliente():
    # El front agrupa las tarjetas por contact_id (una persona = una tarjeta),
    # no por ticket. Debe venir como columna devuelta, no solo en el JOIN.
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    assert "AS contact_id" in query


def test_scored_rows_aligera_payload_de_la_lista():
    # /api/scores traia TODA la cuenta sin paginar: sistemas ~112MB/13s. El
    # rating_rationale (parrafo del LLM) era el 40% del payload y en la lista
    # solo se usa como snippet -> se trunca. Los campos que solo consume el modal
    # de detalle (servido aparte por _DETAIL_SQL) no viajan en la lista.
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    # rationale como snippet truncado, con el mismo alias para el front
    assert "left(cs.rating_rationale" in query.lower()
    assert "AS rating_rationale" in query
    # campos de solo-detalle fuera de la lista (peso muerto). Se mide sobre lo que se DEVUELVE:
    # `agent_message_count` vive dentro del CASE de identidad y ahi no pesa nada en el payload,
    # es un predicado. Sin descontar la expresion, el test prohibiria la cuarta puerta.
    from src.identidad import OPERADOR_O_NADA
    devuelto = query.replace(OPERADOR_O_NADA, "")
    for dead in ("cs.queue_name", "cs.resolved_at", "cs.rubric", "cs.message_count",
                 "cs.agent_message_count", "cs.bot_message_count", "cs.contact_message_count",
                 "cs.first_response_seconds", "cs.resolution_seconds", "cs.was_unassigned"):
        assert dead not in devuelto, f"{dead} deberia salir de la lista"
    # lo que la lista SI usa se mantiene
    for keep in ("cs.stars", "cs.rating_label", "cs.deposit_count", "cs.segment", "AS user_name"):
        assert keep in query, f"{keep} no deberia salir de la lista"


def test_scores_filters_base_solo_cuenta():
    """Sin filtros del usuario queda el scope de cuenta + la baja lógica de operadores
    (siempre presente por default; ver test_scores_filters_esconde_operadores_apagados)."""
    where, params = _scores_filters("datos")
    assert where.startswith("cs.account = %(account)s")
    # ninguna condición del USUARIO se cuela cuando no se pidió ninguna
    for campo in ("cs.eval_status", "cs.motivo", "cs.segment", "t.channel", "cs.stars",
                  "ILIKE", "cs.conversation_created_at"):
        assert campo not in where
    assert params == {"account": "datos"}


def test_scores_filters_aplica_cada_filtro():
    where, params = _scores_filters(
        "sistemas", estado="evaluated", segment="jugador", canal="WHATSAPP",
        op="Virginia", date_from="2026-01-01", date_to="2026-06-30", search="juan",
        motivo="retiro")
    assert "cs.eval_status = %(estado)s" in where and params["estado"] == "evaluated"
    assert "cs.motivo = %(motivo)s" in where and params["motivo"] == "retiro"
    assert "cs.segment = %(segment)s" in where and params["segment"] == "jugador"
    assert "t.channel = %(canal)s" in where and params["canal"] == "WHATSAPP"
    # El filtro compara la MISMA etiqueta que ofrece el desplegable (ver el test de identidad).
    assert f"{OPERADOR_RESUELTO} = %(op)s" in where and params["op"] == "Virginia"
    assert "cs.conversation_created_at >= %(dfrom)s" in where and params["dfrom"] == "2026-01-01"
    assert "cs.conversation_created_at <= %(dto)s" in where and params["dto"] == "2026-06-30"
    # búsqueda: mismos campos que matchBase del front (cliente, número, operador)
    assert "ILIKE %(q)s" in where and params["q"] == "%juan%"


def test_scores_filters_filtra_por_CAUSA_de_sin_evaluar():
    """La tarjeta de 'sin evaluar por causa' pasa a ser CLICABLE, y para eso el filtro
    tiene que saber de causas.

    Hasta el 2026-08-14 no lo sabia: el comentario del front decia textual "NO es clicable
    a proposito: el filtro de estado es Todas/Evaluadas/Sin evaluar y no sabe de causas, asi
    que un clic mostraria TODO lo salteado y no la fila apretada". Ahora la sabe.

    No hace falta tocar `estado`: las filas evaluadas tienen `skip_reason` NULL, asi que el
    predicado ya las excluye solo.
    """
    where, params = _scores_filters("datos", causa="no_agent_reply")
    assert "cs.skip_reason = %(causa)s" in where
    assert params["causa"] == "no_agent_reply"


def test_scores_filters_sin_causa_no_agrega_predicado():
    where, _ = _scores_filters("datos")
    assert "skip_reason" not in where
    where_all, _ = _scores_filters("datos", causa="all")
    assert "skip_reason" not in where_all


def test_scores_filters_causa_compone_con_los_demas():
    where, params = _scores_filters("datos", causa="sin_motivo", segment="jugador")
    assert "cs.skip_reason = %(causa)s" in where
    assert "cs.segment = %(segment)s" in where
    assert params["causa"] == "sin_motivo" and params["segment"] == "jugador"


def test_scores_filters_esconde_operadores_apagados_por_DEFAULT():
    """Baja lógica: un operador apagado desaparece de TODO lo que sale de
    conversation_scores — KPIs incluidos, no solo de los cuadros por operador. En `sistemas`
    son 31 operadores y 27.398 sesiones históricas que ensuciaban promedios y rankings.

    Se filtra con NOT EXISTS contra operator_status y NO con una lista de nombres traída
    desde Python: así `_scores_filters` sigue siendo puro (account + kwargs -> where,
    params) y no necesita un cursor."""
    where, params = _scores_filters("sistemas")
    assert "NOT EXISTS" in where and "operator_status" in where
    assert "os.activo = false" in where
    # matchea por el nombre RESUELTO, el mismo con el que agrupan los cuadros
    assert "coalesce(u.name, cs.user_name)" in where
    # sin parámetros nuevos: el account ya está en el WHERE base
    assert set(params) == {"account"}


def test_scores_filters_incluir_inactivos_saca_el_filtro():
    """La baja es LÓGICA: los datos siguen ahí y tiene que haber forma de verlos. Sin esta
    salida, apagar a alguien sería una eliminación de hecho."""
    where, _ = _scores_filters("sistemas", inactivos="incluir")
    assert "operator_status" not in where


def test_summary_kpis_declara_cuantos_operadores_quedaron_ocultos():
    """Mismo criterio que con las sesiones anómalas: lo que se excluye se DECLARA. Si el
    dashboard esconde 31 operadores sin decirlo, los números mienten por omisión."""
    cur = _FakeCursor(
        rows=[], description=["total", "evaluadas", "no_evaluadas", "avg_stars", "depositos",
                              "dep_conv", "operadores", "depositos_excluidos",
                              "sesiones_excluidas", "operadores_ocultos"],
        one=(120, 100, 20, Decimal("3.20"), 45, 30, 8, 900, 3, 31))
    out = summary_kpis(cur, "sistemas")
    assert out["operadores_ocultos"] == 31
    query, _ = cur.executed[0]
    assert "operadores_ocultos" in query


def test_scores_filters_rating_mapea_label_a_estrella():
    # El front bucketea por estrella: 'buena' = 4★. En SQL se filtra por cs.stars.
    where, params = _scores_filters("datos", rating="buena")
    assert "cs.stars = %(rstars)s" in where
    assert params["rstars"] == 4


def test_summary_kpis_agrega_server_side_scopeado_por_cuenta():
    # KPIs calculados en la BD (no mandando 113k filas). Reproduce renderKpis:
    # total, evaluadas, promedio ★, depósitos, conversaciones con depósito, operadores.
    cur = _FakeCursor(
        rows=[], description=["total", "evaluadas", "avg_stars", "depositos", "dep_conv", "operadores"],
        one=(120, 100, Decimal("3.20"), 45, 30, 8))
    out = summary_kpis(cur, "sistemas")
    query, params = cur.executed[0]
    assert "cs.account = %(account)s" in query and params["account"] == "sistemas"
    assert "FILTER (WHERE cs.eval_status = 'evaluated')" in query
    assert "sum(cs.deposit_count)" in query
    assert "count(DISTINCT" in query           # operadores distintos
    # numeric -> float (evita el bug de string en el JSON)
    assert out["avg_stars"] == 3.2 and isinstance(out["avg_stars"], float)
    assert out["total"] == 120 and out["evaluadas"] == 100 and out["operadores"] == 8
    assert "pendientes" in out  # sesiones cerradas aún sin scorear (backfill en curso)


def test_summary_kpis_excluye_sesiones_anomalas_de_los_depositos():
    """Las sesiones skipeadas por `anomalous_size` (>250 mensajes) son blobs patologicos:
    en prod hay conversaciones de 15.100 mensajes con 3.025 imagenes del cliente. Nunca se
    evaluaron, pero deposit_count SI se calcula y se persiste, y aportaba el 44,7% de los
    comprobantes de `sistemas` (88.496 de 198.027) desde el 0,89% de las sesiones. Los
    agregados de deposito tienen que dejarlas afuera; el resto de los skips se queda
    (customer_media_only = el cliente mando el comprobante y nadie respondio: es real)."""
    cur = _FakeCursor(
        rows=[], description=["total", "evaluadas", "no_evaluadas", "avg_stars", "depositos",
                              "dep_conv", "operadores", "depositos_excluidos", "sesiones_excluidas"],
        one=(120, 100, 20, Decimal("3.20"), 45, 30, 8, 900, 3))
    out = summary_kpis(cur, "sistemas")
    query, _ = cur.executed[0]
    # Se filtra por la PROPIEDAD (tamaño) y no por la etiqueta `skip_reason`: en
    # decide_eligibility el chequeo de 'no_agent_reply' corre ANTES que el de tamaño, así
    # que 16 sesiones de >250 mensajes quedaron etiquetadas 'no_agent_reply' y se escapaban
    # con 2.310 comprobantes. El umbral sale de router.ANOMALOUS_MESSAGE_MAX, no hardcodeado.
    # coalesce: message_count es nullable y un NULL descartaría la fila del FILTER en silencio
    assert f"coalesce(cs.message_count, 0) > {ANOMALOUS_MESSAGE_MAX}" in query
    assert "sum(cs.deposit_count) FILTER (WHERE NOT" in query
    assert "count(*) FILTER (WHERE cs.deposit_count > 0 AND NOT" in query
    # ...y ademas se reporta lo excluido, para poder declararlo en la UI en vez de esconderlo
    assert out["depositos_excluidos"] == 900 and out["sesiones_excluidas"] == 3
    # el filtro de estado tambien se declara: cuantas quedaron sin evaluar
    assert out["no_evaluadas"] == 20


def test_deposit_by_channel_excluye_sesiones_anomalas():
    """Mismo criterio que los KPIs: la tarjeta de % deposito por canal no puede contar
    las sesiones anomalas, o el porcentaje de un canal se dispara por un solo blob."""
    cur = _FakeCursor(rows=[], description=["canal", "n", "dep"])
    deposit_by_channel(cur, "datos")
    query, _ = cur.executed[0]
    # coalesce: message_count es nullable y un NULL descartaría la fila del FILTER en silencio
    assert f"coalesce(cs.message_count, 0) > {ANOMALOUS_MESSAGE_MAX}" in query


def test_pending_sessions_count_gate_6h_y_scope():
    cur = _FakeCursor(one=(42,))
    n = pending_sessions_count(cur, "datos", date_from="2026-07-01", date_to="2026-07-20")
    assert n == 42
    query, params = cur.executed[0]
    assert "FROM conversation_sessions cs" in query
    assert "cs.account = %(account)s" in query and params["account"] == "datos"
    # mismo gate que el worker: cerrada hace >6h y sin score al día
    assert "interval '6 hours'" in query
    assert "s.scored_at >= cs.end_at" in query
    # respeta el rango de fechas sobre start_at
    assert "cs.start_at >= %(dfrom)s" in query and "cs.start_at <= %(dto)s" in query
    assert params["dfrom"] == "2026-07-01" and params["dto"] == "2026-07-20"


def test_pending_sessions_count_sin_fechas_no_agrega_clausula():
    cur = _FakeCursor(one=(7,))
    n = pending_sessions_count(cur, "sistemas")
    assert n == 7
    query, _ = cur.executed[0]
    assert "start_at >=" not in query and "start_at <=" not in query


def test_dist_from_labels_bucketea_por_estrella():
    # Reproduce renderDist: label -> estrella -> bucket. Los labels de bot
    # (funcional=4★) caen en el mismo bucket que su equivalente humano (buena).
    counts = _dist_from_labels([("excelente", 10), ("funcional", 5), ("mala", 2)])
    assert counts == {"excelente": 10, "buena": 5, "aceptable": 0, "deficiente": 0, "mala": 2}


def test_build_ops_agrupa_por_operador_y_ordena_por_volumen():
    rows = [("Ana", "buena", 3, 12.0), ("Ana", "mala", 1, 1.0), ("Beto", "excelente", 5, 25.0)]
    out = _build_ops(rows)
    assert [o["name"] for o in out] == ["Beto", "Ana"]        # orden por volumen desc
    ana = out[1]
    assert ana["n"] == 4 and round(ana["avg"], 2) == 3.25       # (12+1)/4
    assert ana["dist"] == [0, 3, 0, 0, 1]                        # [excelente,buena,aceptable,deficiente,mala]


def test_build_dep_channel_calcula_pct_y_ordena():
    out = _build_dep_channel([("WHATSAPP", 100, 40), ("FACEBOOK", 10, 1)])
    assert out[0] == {"canal": "WHATSAPP", "n": 100, "dep": 40, "pct": 40}
    assert out[1] == {"canal": "FACEBOOK", "n": 10, "dep": 1, "pct": 10}


def test_distribution_ignora_filtro_rating():
    # renderDist usa populationForDist = matchBase SIN el filtro de calificación
    # (para mostrar todas las barras aunque haya un rating seleccionado).
    cur = _FakeCursor(rows=[("buena", 5)], description=["rating_label", "n"])
    distribution(cur, "datos", rating="excelente", segment="jugador")
    query, params = cur.executed[0]
    assert "cs.stars" not in query                 # rating stripped
    assert "cs.segment = %(segment)s" in query      # otros filtros sí
    assert "cs.eval_status = 'evaluated'" in query
    assert "rstars" not in params


def test_operators_table_agrupa_solo_con_operador_y_evaluadas():
    cur = _FakeCursor(rows=[], description=["op", "rating_label", "n", "sum_stars"])
    operators_table(cur, "sistemas")
    query, _ = cur.executed[0]
    assert "'Operador sin identificar'" in query
    assert "cs.eval_status = 'evaluated'" in query
    assert "u.name IS NOT NULL OR" in query          # excluye filas sin operador


def test_deposit_by_channel_sql():
    cur = _FakeCursor(rows=[], description=["canal", "n", "dep"])
    deposit_by_channel(cur, "datos")
    query, _ = cur.executed[0]
    # el conteo sigue siendo por deposit_count>0; el filtro extra de sesiones anómalas
    # lo cubre test_deposit_by_channel_excluye_sesiones_anomalas.
    assert "FILTER (WHERE cs.deposit_count > 0" in query
    assert "GROUP BY 1" in query


def test_build_motivo_stats_ordena_por_volumen_y_avg_none_sin_evaluadas():
    # (motivo, n, evaluadas, avg_stars) -> ordenado por n desc; avg None si 0 evaluadas.
    rows = [
        ("info", 5, 4, 3.5),
        ("deposito", 20, 20, 3.0),
        ("promo", 2, 0, None),
    ]
    out = _build_motivo_stats(rows)
    assert [o["motivo"] for o in out] == ["deposito", "info", "promo"]
    assert out[0] == {"motivo": "deposito", "n": 20, "evaluadas": 20, "avg": 3.0}
    assert out[2]["avg"] is None


# `sin_motivo` NO es un motivo, es la AUSENCIA de uno — la misma decision del negocio del
# 2026-08-07 que ya rige en _QUALITY_MOTIVO_SQL, que hasta ahora NO se habia aplicado a esta
# tarjeta (seguia con el `coalesce(cs.motivo,'sin_motivo')`). Parecia arreglado en `sistemas`
# de casualidad: ahi las filas sin motivo son casi todas del segmento `agente` (6.158 de
# 6.687) y el filtro de AMBIENTE ya las barria. En `datos`, que no tiene agente, sus 712
# filas son sesiones `jugador` SALTEADAS y el ambiente no las toca -> la fila quedaba visible.
# Y el label mentia doble: de las 710 de `datos`, solo 450 son `skip_reason='sin_motivo'`;
# las otras 260 son customer_media_only (188), no_agent_reply (51), anomalous_size (12) y
# demas — todo lo salteado metido en una bolsa con el nombre equivocado.

def test_build_motivo_stats_descarta_sin_motivo_en_cualquier_cuenta():
    rows = [
        ("deposito", 20, 20, 3.0),
        ("sin_motivo", 712, 0, None),   # `datos`: salteadas de jugador
        ("info", 5, 4, 3.5),
    ]
    out = _build_motivo_stats(rows)
    assert [o["motivo"] for o in out] == ["deposito", "info"]


def test_build_motivo_stats_descarta_el_motivo_nulo():
    # Guard en el builder ademas del SQL: si alguien vuelve a meter el coalesce -o saca el
    # `IS NOT NULL`- la tarjeta no se rompe. Mismo patron que _build_quality_motivo.
    out = _build_motivo_stats([("deposito", 3, 3, 4.0), (None, 9, 0, None)])
    assert [o["motivo"] for o in out] == ["deposito"]


def test_quality_motivo_sql_y_motivo_stats_sql_coinciden_en_excluir_sin_motivo():
    # Las dos tarjetas de motivo tienen que contar la MISMA poblacion: si una incluye la
    # ausencia de motivo y la otra no, los totales no cierran entre cuadros.
    from src.queries import _MOTIVO_STATS_SQL, _QUALITY_MOTIVO_SQL
    for sql in (_MOTIVO_STATS_SQL, _QUALITY_MOTIVO_SQL):
        assert "cs.motivo IS NOT NULL" in sql
        assert "sin_motivo" not in sql


def test_build_motivo_cobertura_separa_la_frontera_del_agujero():
    # La tarjeta de motivo promedia SOLO las sesiones con motivo, y en `sistemas` esas son 39
    # de 135 (medido el 2026-08-12). Sin declararlo, la tarjeta se lee como si cubriera todo.
    # Pero el 71% que falta NO es un agujero: son las 96 del segmento `agente`, donde el
    # motivo es NULL POR DISEÑO (se califican por agilidad, sin LLM). Las dos causas van
    # separadas justamente para eso: si `sin_motivo_otro` sube de cero, ESO si es un bug.
    from src.queries import _build_motivo_cobertura
    out = _build_motivo_cobertura((135, 39, 96, 0))
    assert out == {"evaluadas": 135, "con_motivo": 39, "sin_motivo_agente": 96,
                   "sin_motivo_otro": 0, "pct": 29}
    # cuenta sin segmento agente: cobertura total, nada que aclarar
    assert _build_motivo_cobertura((117, 117, 0, 0))["pct"] == 100
    # division por cero: una cuenta recien creada no puede tumbar la tarjeta
    assert _build_motivo_cobertura((0, 0, 0, 0))["pct"] == 0


def test_motivo_cobertura_cuenta_LA_MISMA_poblacion_que_la_tarjeta():
    # Si la cobertura cuenta una poblacion distinta de la que promedia, el porcentaje miente.
    from src.queries import _MOTIVO_COBERTURA_SQL, _MOTIVO_STATS_SQL, _build_motivo_cobertura
    for sql in (_MOTIVO_COBERTURA_SQL, _MOTIVO_STATS_SQL):
        assert "cs.eval_status = 'evaluated'" in sql or "eval_status = 'evaluated'" in sql
    # La cobertura filtra por motivo dentro de los FILTER (ahi va), NUNCA en el WHERE: su
    # trabajo es contar lo que queda AFUERA de la tarjeta.
    assert "\n   AND cs.motivo IS NOT NULL" not in _MOTIVO_COBERTURA_SQL
    # Y los tres cajones tienen que SUMAR lo evaluado. Si el SQL se toca y dejan de cerrar,
    # el porcentaje miente sobre una poblacion que ya no existe: mejor que reviente aca.
    import pytest
    with pytest.raises(ValueError):
        _build_motivo_cobertura((135, 39, 90, 0))


def test_build_ops_motivo_matriz_top_y_celdas():
    # filas (op, motivo, n, avg_stars) -> matriz operador x motivo, top por volumen.
    rows = [
        ("Ana", "deposito", 30, 3.1), ("Ana", "info", 5, 4.0),
        ("Luis", "retiro", 10, 2.8),
    ]
    out = _build_ops_motivo(rows, top_n=10)
    assert out["motivos"] == ["deposito", "info", "retiro"]
    ana = next(o for o in out["operators"] if o["name"] == "Ana")
    assert ana["n"] == 35 and out["operators"][0]["name"] == "Ana"   # más volumen primero
    assert ana["cells"]["deposito"] == {"n": 30, "avg": 3.1}
    assert "retiro" not in ana["cells"]


class _FakeCursorPorLargo(_FakeCursor):
    """`fetchone` segun cuantas columnas pide la consulta: `summary` llama a DOS agregados de
    una sola fila (KPIs de 6 columnas y cobertura de 4) y una tupla fija no sirve para ambas."""

    def fetchone(self):
        query = self.executed[-1][0] if self.executed else ""
        return (0, 0, 0, 0) if "sin_agente" in query else (0, 0, None, 0, 0, 0)


def test_summary_combina_las_secciones():
    cur = _FakeCursorPorLargo(rows=[], description=["total", "evaluadas", "avg_stars", "depositos", "dep_conv", "operadores"])
    out = summary(cur, "datos")
    assert set(out) == {"kpis", "distribution", "operators", "deposit_by_channel",
                        "quality_evolution", "motivo_stats", "motivo_cobertura",
                        "skip_stats", "ops_motivo", "quality_motivo"}


def test_build_quality_evolution_top_n_avg_y_umbral_min():
    # (mes, op, n, sum_stars). MIN=2 aquí: mes-op con <2 convs -> None.
    rows = [("2026-01", "Ana", 4, 16.0), ("2026-02", "Ana", 1, 5.0),
            ("2026-01", "Beto", 2, 6.0)]
    out = _build_quality_evolution(rows, top_n=8, min_conv=2)
    assert out["months"] == ["2026-01", "2026-02"]
    ana = next(o for o in out["operators"] if o["name"] == "Ana")
    assert ana["data"] == [4.0, None]     # ene 16/4=4.0; feb 1<2 conv -> None
    beto = next(o for o in out["operators"] if o["name"] == "Beto")
    assert beto["data"] == [3.0, None]    # ene 6/2=3.0; feb sin datos -> None


def test_build_quality_motivo_lineas_por_operador_y_promedio():
    # (mes, motivo, op, n, sum_stars). op_min_conv=2, avg_min_conv=3.
    rows = [
        ("2026-01", "deposito", "Ana",  3, 12.0),   # 4.0
        ("2026-01", "deposito", "Beto", 1,  2.0),   # 1<2 -> None por operador, pero suma al promedio
        ("2026-01", "info",     "Ana",  2,  6.0),   # 3.0
    ]
    out = _build_quality_motivo(rows, top_n=6, op_min_conv=2, avg_min_conv=3)
    assert out["months"] == ["2026-01"]
    # motivos ordenados: deposito antes que info (orden canónico de la rúbrica)
    assert [m["motivo"] for m in out["motivos"]] == ["deposito", "info"]
    dep = out["motivos"][0]
    ana = next(o for o in dep["operators"] if o["name"] == "Ana")
    beto = next(o for o in dep["operators"] if o["name"] == "Beto")
    assert ana["data"] == [4.0]            # 12/3
    assert beto["data"] == [None]          # 1 < op_min_conv=2
    assert dep["avg"] == [round(14.0 / 4, 2)]  # promedio del motivo: (12+2)/(3+1)=3.5, 4>=3


def test_filter_options_devuelve_listas_por_cuenta():
    # Los desplegables (segmento/canal/operador) salían de DATA en el front; ahora
    # del server, sin filtrar y scopeado por cuenta.
    cur = _FakeCursor(rows=[("a",), ("b",)], description=[])
    out = filter_options(cur, "datos")
    assert set(out) == {"segments", "channels", "operators", "motivos"}
    assert out["segments"] == ["a", "b"]
    # las 4 consultas: DISTINCT, ORDER, scopeadas por cuenta (segmento/canal/operador/motivo)
    assert len(cur.executed) == 4
    for query, params in cur.executed:
        assert "DISTINCT" in query and "ORDER BY" in query
        assert params["account"] == "datos"


def test_sort_convs_replica_sortconvs_del_front():
    convs = [{"conversation_created_at": "2026-03-01", "stars": 2},
             {"conversation_created_at": "2026-01-01", "stars": 5},
             {"conversation_created_at": "2026-02-01", "stars": None}]
    assert [c["conversation_created_at"] for c in _sort_convs(convs, "new")] == ["2026-03-01", "2026-02-01", "2026-01-01"]
    assert [c["conversation_created_at"] for c in _sort_convs(convs, "old")] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    # worst = estrella asc, sin evaluar (None->99) al final
    assert [c["stars"] for c in _sort_convs(convs, "worst")] == [2, 5, None]
    # best = estrella desc, None->99 va primero (igual que el front: stars??99)
    assert [c["stars"] for c in _sort_convs(convs, "best")] == [None, 5, 2]


def test_ticket_cards_agrupa_convs_por_card_key():
    card_rows = [{"card_key": "c1", "n": 2, "visitas": 1, "avg_stars": 3.5,
                  "last_at": "2026-03-01", "cust": "Ana", "num": "593...", "ch": "WHATSAPP", "total": 1}]
    conv_rows = [{"card_key": "c1", "conversation_id": "x", "conversation_created_at": "2026-01-01", "stars": 3},
                 {"card_key": "c1", "conversation_id": "y", "conversation_created_at": "2026-03-01", "stars": 4}]
    cards = _ticket_cards(card_rows, conv_rows, "new")
    assert len(cards) == 1
    c = cards[0]
    assert c["cust"] == "Ana" and c["n"] == 2 and c["visitas"] == 1 and c["avg"] == 3.5
    assert [cv["conversation_id"] for cv in c["convs"]] == ["y", "x"]   # ordenadas (new = fecha desc)


def test_tickets_page_pagina_ordena_y_agrupa():
    cur = _FakeCursor(rows=[], description=["card_key", "n", "visitas", "avg_stars", "last_at", "cust", "num", "ch", "total"])
    out = tickets_page(cur, "sistemas", page=2, sort="best", page_size=12)
    query, params = cur.executed[0]
    assert "GROUP BY card_key" in query
    assert "avg_stars DESC NULLS LAST" in query            # sort=best
    assert "LIMIT %(limit)s OFFSET %(offset)s" in query
    assert params["limit"] == 12 and params["offset"] == 12   # página 2
    # card_key: conversation_id/ticket_id son uuid -> COALESCE exige castear ambos
    # a text (COALESCE(text, uuid) revienta en Postgres). Regresión del 500.
    assert "cs.conversation_id::text" in query
    assert out == {"cards": [], "total": 0, "page": 2, "pages": 1, "page_size": 12}


def test_build_load_series_top_n_y_otros_alineado_a_meses():
    rows = [("2026-01", "A", 5), ("2026-01", "B", 3), ("2026-02", "A", 2),
            ("2026-01", "C", 1), ("2026-02", "C", 1)]
    out = _build_load_series(rows, top_n=2)
    assert out["months"] == ["2026-01", "2026-02"]
    ops = [s["op"] for s in out["series"]]
    assert ops == ["A", "B", "Otros"]                    # A(7) B(3) top-2; C(2) -> Otros
    a = next(s for s in out["series"] if s["op"] == "A")
    assert a["data"] == [5, 2]                            # alineado a los meses
    otros = next(s for s in out["series"] if s["op"] == "Otros")
    assert otros["data"] == [1, 1]                        # meses sin dato -> 0


def test_build_load_series_sin_otros_si_no_sobran():
    out = _build_load_series([("2026-01", "A", 4)], top_n=7)
    assert [s["op"] for s in out["series"]] == ["A"]      # no aparece 'Otros' vacío


def test_build_pct_series_calcula_pct_y_omite_bajo_volumen():
    rows = [("2026-01", "A", 10, 5), ("2026-02", "A", 4, 4)]
    out = _build_pct_series(rows, top_n=7, min_conv=8)
    a = out["series"][0]
    assert a["op"] == "A"
    assert a["data"] == [50.0, None]         # ene 5/10=50%; feb 4<8 -> None (omitido)


def test_build_pct_series_otros_agrega_conv_y_dep_del_resto():
    rows = [("2026-01", "A", 100, 50), ("2026-01", "B", 10, 1), ("2026-01", "C", 10, 9)]
    out = _build_pct_series(rows, top_n=1, min_conv=8)
    assert [s["op"] for s in out["series"]] == ["A", "Otros"]
    otros = next(s for s in out["series"] if s["op"] == "Otros")
    assert otros["data"] == [50.0]           # (1+9)/(10+10) = 50%


def test_build_new_vs_deposit_ordena_y_calcula_pct():
    # filas: (mes, conv, con_dep, nuevos, nuevos_con_dep)
    rows = [("2026-02", 50, 10, 30, 6), ("2026-01", 100, 42, 57, 19)]
    out = _build_new_vs_deposit(rows)
    assert out["months"] == ["2026-01", "2026-02"]        # ordenado por mes
    assert out["nuevos"] == [57, 30]
    assert out["pct"] == [42.0, 20.0]                      # 42/100 y 10/50


def test_build_new_vs_deposit_expone_el_pct_DE_LOS_NUEVOS():
    """El cuadro dibuja barras de jugadores NUEVOS y una línea de % depósito, pero ese %
    se calcula sobre TODAS las conversaciones del segmento, no sobre los nuevos: son
    denominadores distintos y la leyenda ('línea = % que depositó') hacía leer el segundo
    como si fuera el primero. En prod, julio de `sistemas`: 60,4% graficado contra 33,8%
    real de los nuevos. Se agrega la serie coherente con las barras, sin perder la vieja."""
    rows = [("2026-01", 100, 42, 57, 19), ("2026-02", 50, 10, 30, 6)]
    out = _build_new_vs_deposit(rows)
    # 19 de 57 nuevos y 6 de 30 nuevos
    assert out["pct_nuevos"] == [33.3, 20.0]
    # y la serie global sigue estando, con su propio denominador
    assert out["pct"] == [42.0, 20.0]


def test_build_new_vs_deposit_sin_nuevos_no_divide_por_cero():
    out = _build_new_vs_deposit([("2026-03", 10, 4, 0, 0)])
    assert out["pct_nuevos"] == [None]      # None, no 0.0: "no hubo nuevos" != "0% depositó"
    assert out["pct"] == [40.0]


class _CursorSecuencia:
    """fetchall() devuelve una tanda distinta por llamada. Necesario para los cuadros
    full-scale: la 1ra la consume `_jugador_queue_ids` (filas de 2) y la 2da el builder
    (filas de 3 o 4). Con un solo set de filas el builder revienta al desempacar."""

    def __init__(self, *tandas):
        self._tandas = list(tandas)
        self.executed = []
        self.description = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return self

    def fetchall(self):
        return self._tandas.pop(0) if self._tandas else []

    def fetchone(self):
        # Misma secuencia que fetchall: consume la tanda y devuelve su primera fila. La
        # necesitan las funciones que resuelven colas (fetchall) y despues cuentan (fetchone).
        tanda = self._tandas.pop(0) if self._tandas else []
        return tanda[0] if tanda else None


def test_charts_full_scale_tambien_esconden_operadores_apagados():
    """Los dos cuadros de /api/charts NO pasan por `_scores_filters` (van full-scale sobre
    conversations), así que necesitan su propia cláusula. Si no, un operador apagado
    desaparecía de todo el dashboard MENOS de estos dos — el peor de los dos mundos."""
    for fn in (load_by_operator, deposit_pct_by_operator):
        cur = _CursorSecuencia([("q1", "Jugadores")], [])
        fn(cur, "sistemas")
        query = cur.executed[-1][0]
        assert "operator_status" in query, f"{fn.__name__} no filtra apagados"
        assert "os.activo = false" in query
        # acá el nombre se resuelve SOLO por users.name (no hay conversation_scores), y el
        # fallback tiene que ser el MISMO string que guarda operator_status.
        assert "'Operador sin identificar'" in query


def test_charts_full_scale_con_inactivos_incluir_no_filtran():
    for fn in (load_by_operator, deposit_pct_by_operator):
        cur = _CursorSecuencia([("q1", "Jugadores")], [])
        fn(cur, "sistemas", inactivos="incluir")
        assert "operator_status" not in cur.executed[-1][0]


def test_conversion_where_esconde_apagados_y_deja_la_salida():
    """La conversión agrega player_conversions y resuelve el operador por pc.user_id, otra
    expresión más. Mismo criterio: se esconden por default, con salida explícita."""
    where, _ = _conversion_where("sistemas")
    assert "operator_status" in where and "os.activo = false" in where
    where2, _ = _conversion_where("sistemas", inactivos="incluir")
    assert "operator_status" not in where2


def test_conversion_where_solo_filtros_que_aplican_al_potencial():
    where, params = _conversion_where(
        "datos", canal="WHATSAPP", segment="jugador", op="Virginia",
        date_from="2026-01-01", date_to="2026-06-30",
        estado="evaluated", rating="buena", search="x")  # estos 3 se ignoran
    assert "pc.channel = %(canal)s" in where and params["canal"] == "WHATSAPP"
    assert "pc.segment = %(segment)s" in where
    assert "%(op)s" in where and params["op"] == "Virginia"
    assert "pc.first_at >= %(dfrom)s" in where and "pc.first_at <= %(dto)s" in where
    assert "estado" not in params and "rating" not in params and "search" not in params


def test_build_conversion_ranking_orden_pct_bot_otros_y_returned():
    # filas: (op, potential, converted[depósito], returned[re-engagement])
    rows = [("Virginia", 100, 30, 40), ("Ana", 100, 5, 10), ("Poco", 3, 3, 2),
            ("BOT / sin operador", 200, 12, 20)]
    out = _build_conversion_ranking(rows, min_potential=8)
    ops = out["operators"]
    assert [o["op"] for o in ops] == ["Virginia", "Ana", "Otros", "BOT / sin operador"]
    assert ops[0]["pct"] == 30.0                              # ranking por tasa de depósito desc
    assert ops[0]["returned"] == 40 and ops[0]["ret_pct"] == 40.0
    otros = next(o for o in ops if o["op"] == "Otros")
    assert otros["converted"] == 3 and otros["returned"] == 2   # <8 agregados
    bot = ops[-1]
    assert bot["converted"] == 12 and bot["returned"] == 20     # bot aparte, al final
    assert out["total_potential"] == 403 and out["total_converted"] == 50 and out["pct"] == 12.4
    assert out["total_returned"] == 72 and out["ret_pct"] == 17.9


def test_build_conversion_by_month_ordena_pct_y_returned():
    out = _build_conversion_by_month([("2026-02", 50, 10, 15), ("2026-01", 100, 20, 30)])
    assert out["months"] == ["2026-01", "2026-02"]
    assert out["potential"] == [100, 50] and out["converted"] == [20, 10]
    assert out["pct"] == [20.0, 20.0]
    assert out["returned"] == [30, 15]
    assert out["ret_pct"] == [30.0, 30.0]


def test_conversion_by_operator_sql_agrega_player_conversions():
    cur = _FakeCursor(rows=[], description=[])
    conversion_by_operator(cur, "sistemas", canal="WHATSAPP")
    query, params = cur.executed[0]
    assert "FROM player_conversions pc" in query
    assert "FILTER (WHERE pc.deposited)" in query
    assert "'BOT / sin operador'" in query and "LEFT JOIN users u" in query
    assert "GROUP BY 1" in query and params["canal"] == "WHATSAPP"


def test_conversion_by_month_sql():
    cur = _FakeCursor(rows=[], description=[])
    conversion_by_month(cur, "datos")
    query, _ = cur.executed[0]
    assert "to_char(pc.first_at, 'YYYY-MM')" in query and "GROUP BY 1" in query


def test_build_conversion_passivity_denominadores_distintos():
    # conv% sobre total; pasiva% sobre CLASIFICADAS. Mes con <min -> None.
    rows = [("2026-01", "Ana", 10, 3, 8, 4), ("2026-02", "Ana", 3, 1, 2, 1)]
    out = _build_conversion_passivity(rows, top_n=8, min_conv=5)
    assert out["months"] == ["2026-01", "2026-02"]
    ana = out["operators"][0]
    assert ana["name"] == "Ana"
    assert ana["conv"] == [30.0, None]      # 3/10; feb n=3<5 -> None
    assert ana["pasiva"] == [50.0, None]    # 4/8 clasif; feb clasif=2<5 -> None


def test_conversion_passivity_sql_conv_y_pasiva():
    cur = _FakeCursor(rows=[], description=[])
    conversion_passivity_evolution(cur, "sistemas")
    query, _ = cur.executed[0]
    assert "FILTER (WHERE pc.deposited)" in query
    assert "FILTER (WHERE pc.attention = 'pasivo')" in query
    assert "FILTER (WHERE pc.attention IS NOT NULL)" in query   # denominador de pasiva
    assert "JOIN users u" in query and "pc.user_id IS NOT NULL" in query


def test_conversion_cohort_lista_con_llave_de_drilldown():
    cur = _FakeCursor(rows=[], description=[])
    conversion_cohort(cur, "sistemas", op="Virginia")
    query, params = cur.executed[0]
    assert "FROM player_conversions pc" in query
    assert "pc.first_conversation_id" in query          # llave para abrir la conversación
    assert "ORDER BY pc.first_at DESC" in query and "LIMIT 500" in query
    assert "%(op)s" in query and params["op"] == "Virginia"


def test_conversation_detail_coacciona_decimal_a_numero():
    cur = _FakeCursor(rows=[], description=["conversation_id", "stars"], one=("c1", Decimal("4")))
    d = conversation_detail(cur, "c1")
    assert d["stars"] == 4.0 and isinstance(d["stars"], float)


def test_conversation_detail_filtra_por_id_y_agrega_transcript():
    # fetchone -> fila de detalle; fetchall -> mensajes (vacio aqui)
    cur = _FakeCursor(rows=[], description=["conversation_id"], one=("c1",))
    d = conversation_detail(cur, "c1")
    query, params = cur.executed[0]
    assert "conversation_id = %(cid)s" in query
    assert params["cid"] == "c1"
    assert d["conversation_id"] == "c1"
    assert d["transcript"] == []


def test_conversation_detail_trae_atencion_deposito_motivo_no_rating_applicable():
    # v2: rating_applicable quedó muerto (Opción B retirada) -> se sacó del payload.
    cur = _FakeCursor(rows=[], description=["conversation_id"], one=("c1",))
    conversation_detail(cur, "c1")
    query, _ = cur.executed[0]
    assert "cs.atencion" in query and "cs.deposit_observed" in query and "cs.motivo" in query
    assert "cs.rating_applicable" not in query


def test_tickets_convs_sql_trae_atencion_motivo_no_rating_applicable():
    assert "cs.atencion" in _TICKETS_CONVS_SQL and "cs.motivo" in _TICKETS_CONVS_SQL
    assert "cs.rating_applicable" not in _TICKETS_CONVS_SQL


class _DetailCur:
    """Cursor mínimo: execute no-op, fetchone devuelve `row` (None = sin score)."""
    description = []

    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


def test_fast_close_sql_condiciones():
    from src.queries import _FAST_CLOSE_SQL
    q = _FAST_CLOSE_SQL
    assert "eval_status = 'evaluated'" in q
    assert "cs.stars <= 2" in q                       # sin resolver
    assert "interval '10 minutes'" in q               # cierre rápido
    assert "conversation_sessions" in q


def test_detail_sql_trae_session_seconds():
    from src.queries import _DETAIL_SQL
    assert "session_seconds" in _DETAIL_SQL
    assert "conversation_sessions ses" in _DETAIL_SQL


def test_deposit_mismatch_sql_y_detalle():
    from src.queries import _DEPOSIT_MISMATCH_SQL, _DETAIL_SQL
    assert "cs.deposit_mismatch = true" in _DEPOSIT_MISMATCH_SQL
    assert "cs.deposit_mismatch" in _DETAIL_SQL


def test_conversation_detail_sin_score_devuelve_transcript_pendiente(monkeypatch):
    import src.queries as q
    monkeypatch.setattr(q, "fetch_messages", lambda cur, cid: [
        {"from_me": False, "is_note": False, "body": "hola", "sent_from": None, "media_type": None}])
    d = q.conversation_detail(_DetailCur(None), "conv-x")
    assert d is not None
    assert d["pending"] is True and d["eval_status"] is None
    assert d["transcript"][0]["role"] == "CLIENTE"


def test_conversation_detail_sin_score_ni_mensajes_es_none(monkeypatch):
    import src.queries as q
    monkeypatch.setattr(q, "fetch_messages", lambda cur, cid: [])
    assert q.conversation_detail(_DetailCur(None), "conv-x") is None


# --- tiempos de CIERRE en el detalle (2026-08-06) ----------------------------
# El negocio pidio ver "cuanto demoro en cerrar luego de eso": el hueco entre la
# ultima accion del operador y el cierre del ticket, y el hueco entre que chequeo si
# faltaba algo y el cierre. Medido: la mediana de la puerta es 0,0 min y el 81,8%
# cierra en menos de un minuto, asi que es informacion que hoy no se ve en ningun lado.

def test_detail_sql_trae_los_tiempos_de_cierre():
    from src.queries import _DETAIL_SQL
    assert "cierre_seconds" in _DETAIL_SQL
    assert "algo_mas_cierre_seconds" in _DETAIL_SQL


def test_detail_sql_reusa_el_patron_de_algo_mas_de_signals():
    # Fuente unica: si el patron cambia en signals, el SQL lo sigue solo.
    from src.queries import _DETAIL_SQL
    assert "%(algo_mas_re)s" in _DETAIL_SQL


def test_conversation_detail_pasa_el_patron_como_parametro():
    from src.signals import ANYTHING_ELSE_PATTERN
    from src.queries import conversation_detail
    cur = _FakeCursor([], description=[])
    conversation_detail(cur, "conv-1")
    _, params = cur.executed[0]
    assert params["algo_mas_re"] == ANYTHING_ELSE_PATTERN


def test_ningun_sql_tiene_un_porcentaje_suelto():
    """psycopg parsea el SQL COMPLETO buscando placeholders, comentarios incluidos.

    Un '%' que no sea parte de %(nombre)s ni de un '%%' escapado revienta en runtime
    con "incomplete placeholder", y el cursor falso de estos tests NO lo detecta porque
    no parsea nada. Paso de verdad: un comentario que decia "el 81,8% cierra en menos de
    un minuto" tiro abajo el detalle del chat contra la BD real.

    AMPLIADO el 2026-08-11 a TODO src/: antes solo miraba las constantes `*_SQL` de
    src.queries, y volvi a meter el mismo bug en `src/operators_status._ADMIN_ROWS` -- que no
    termina en _SQL y vive en otro modulo. Un comentario con "9,6%" rompio el modal de
    operadores contra la BD real, con la suite entera en verde. Ahora se barre cualquier
    constante de cualquier modulo que parezca SQL.
    """
    import importlib
    import pathlib
    import re

    sospechosos = []
    raiz = pathlib.Path(__file__).resolve().parents[1] / "src"
    for archivo in sorted(raiz.glob("*.py")):
        mod = importlib.import_module(f"src.{archivo.stem}")
        for nombre in dir(mod):
            valor = getattr(mod, nombre)
            if not isinstance(valor, str) or "%" not in valor:
                continue
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE)\b", valor, re.I):
                continue
            # Se sacan los placeholders VALIDOS -- nombrados `%(x)s` y posicionales `%s`, que
            # usan los executemany de sessions/operators_status -- y los '%%' escapados.
            # Lo que quede es un '%' de texto y revienta en runtime.
            limpio = re.sub(r"%\([a-zA-Z_]+\)s|%s", "", valor).replace("%%", "")
            if "%" in limpio:
                sospechosos.append(f"src.{archivo.stem}.{nombre}")
    assert not sospechosos, f"'%' suelto en: {sospechosos}"


def test_la_lista_de_interacciones_trae_el_reloj():
    # Es el eje de seis de las siete rubricas: sin el hay que abrir sesion por sesion
    # para encontrar las lentas.
    from src.queries import _TICKETS_CONVS_SQL
    assert "cs.first_response_seconds" in _TICKETS_CONVS_SQL


# --- AMBIENTE: el switch jugador / agente / sin_clasificar ----------------------
# Jerarquia del negocio (2026-08-07): manda OPERADORES (ya vigente via `inactivos` en los
# dos mundos) y adentro el AMBIENTE. Antes de esto los cuadros de /api/charts estaban
# clavados a las colas jugador, que son el 24,2% de los comprobantes de deposito: el 71,7%
# lo carrean los agentes y no se veia en ningun cuadro.

def test_scores_filters_traduce_el_ambiente_a_los_segmentos():
    where, params = _scores_filters("sistemas", ambiente="sin_clasificar")
    assert "cs.segment = ANY(%(amb_segments)s)" in where
    assert set(params["amb_segments"]) == {"interno", "marketing", "otro", "descartar"}


def test_scores_filters_con_ambiente_todos_no_filtra_segmento():
    where, params = _scores_filters("sistemas", ambiente="todos")
    assert "amb_segments" not in params
    assert "cs.segment" not in where


def test_ambiente_y_segmento_COMPONEN():
    # El ambiente es el agrupador grueso; el filtro fino de segmento sigue vivo adentro
    # (p. ej. aislar `marketing` dentro de sin_clasificar). Los dos se aplican con AND.
    where, params = _scores_filters("sistemas", ambiente="sin_clasificar", segment="marketing")
    assert "cs.segment = ANY(%(amb_segments)s)" in where
    assert "cs.segment = %(segment)s" in where
    assert params["segment"] == "marketing"


def test_los_cuadros_resuelven_las_colas_DEL_AMBIENTE_pedido():
    # La 1ra tanda es la lista de colas de la cuenta; el resolvedor se queda con las del
    # ambiente. 'Agente 👨👩' es agente, 'Jugadores' es jugador.
    for fn in (load_by_operator, deposit_pct_by_operator):
        cur = _CursorSecuencia([("q1", "Jugadores"), ("q2", "Agente 👨👩")], [])
        fn(cur, "sistemas", ambiente="agente")
        assert cur.executed[-1][1]["qids"] == ["q2"], fn.__name__


def test_el_ambiente_jugador_NO_arrastra_las_conversaciones_sin_cola():
    cur = _CursorSecuencia([("q1", "Jugadores")], [])
    load_by_operator(cur, "sistemas", ambiente="jugador")
    assert "queue_id IS NULL" not in cur.executed[-1][0]


def test_los_ambientes_con_cola_vacia_SUMAN_las_conversaciones_sin_cola():
    # En la BD no hay ninguna cola de nombre vacio: las 16.910 sesiones de "cola vacia"
    # son `queue_id IS NULL`, inalcanzables por lista de colas. Sin este OR, el ambiente
    # sin_clasificar saldria vacio y 'todos' perderia 10.939 conversaciones EN SILENCIO.
    for ambiente in ("sin_clasificar", "todos"):
        cur = _CursorSecuencia([("q1", "Jugadores")], [])
        load_by_operator(cur, "sistemas", ambiente=ambiente)
        assert "queue_id IS NULL" in cur.executed[-1][0], ambiente


def test_sin_colas_propias_pero_CON_las_sin_cola_igual_consulta():
    # Caso borde real: una cuenta sin colas de marketing/otro. qids queda vacio, pero
    # sin_clasificar TIENE contenido (las sin cola) -> no puede cortar devolviendo vacio.
    cur = _CursorSecuencia([("q1", "Jugadores")], [("2026-07", "Mario", 5)])
    r = load_by_operator(cur, "sistemas", ambiente="sin_clasificar")
    assert len(cur.executed) == 2, "corto antes de consultar los datos"
    assert r["months"] == ["2026-07"]


def test_sin_colas_y_sin_las_sin_cola_si_corta_vacio():
    # jugador en una cuenta sin colas de jugador: ahi si no hay nada que consultar.
    cur = _CursorSecuencia([("q1", "Agente 👨👩")], [])
    r = load_by_operator(cur, "sistemas", ambiente="jugador")
    assert r == {"months": [], "series": []}
    assert len(cur.executed) == 1, "consulto datos sin colas que consultar"


def test_el_default_de_los_cuadros_sigue_siendo_jugador():
    # Compatibilidad: quien no pasa ambiente ve lo mismo que veia antes.
    cur = _CursorSecuencia([("q1", "Jugadores"), ("q2", "Agente 👨👩")], [])
    load_by_operator(cur, "sistemas")
    assert cur.executed[-1][1]["qids"] == ["q1"]


def test_new_vs_deposit_tambien_respeta_el_ambiente():
    cur = _CursorSecuencia([("q1", "Jugadores"), ("q2", "Agente 👨👩")], [])
    new_vs_deposit_by_month(cur, "sistemas", ambiente="agente")
    assert cur.executed[-1][1]["qids"] == ["q2"]


def test_composicion_dice_QUE_COMPONE_cada_ambiente():
    # Las colas reales de `sistemas` con sus conversaciones medidas el 2026-08-07.
    cur = _FakeCursor([("Jugadores", 36763), ("Agente 👨👩", 78968), ("", 21546),
                       ("Departamento de Makerting", 849), ("Prueba", 6)])
    amb = ambiente_composition(cur, "sistemas")["ambientes"]
    assert amb["jugador"]["conversaciones"] == 36763
    assert amb["agente"]["conversaciones"] == 78968
    assert amb["sin_clasificar"]["conversaciones"] == 21546 + 849 + 6
    assert amb["todos"]["conversaciones"] == 36763 + 78968 + 21546 + 849 + 6


def test_la_composicion_nombra_la_cola_sin_asignar_por_lo_que_SABEMOS():
    # El codigo la clasifica 'interno', pero el 90% tiene mensajes de cliente reales.
    # La etiqueta dice lo que se sabe (no hay cola), no lo que se supone (es interno).
    cur = _FakeCursor([("", 21546)])
    colas = ambiente_composition(cur, "sistemas")["ambientes"]["sin_clasificar"]["colas"]
    assert colas[0]["cola"] == SIN_COLA_LABEL
    assert colas[0]["segmento"] == "interno"


def test_la_composicion_ordena_las_colas_por_peso():
    cur = _FakeCursor([("ModoSorti", 2102), ("Jugadores", 36763), ("sortiGO", 1473)])
    colas = ambiente_composition(cur, "sistemas")["ambientes"]["jugador"]["colas"]
    assert [c["cola"] for c in colas] == ["Jugadores", "ModoSorti", "sortiGO"]


def test_la_composicion_lista_los_segmentos_presentes():
    cur = _FakeCursor([("", 10), ("Departamento de Makerting", 5)])
    segs = ambiente_composition(cur, "sistemas")["ambientes"]["sin_clasificar"]["segmentos"]
    # solo los PRESENTES, en el orden que define el ambiente (no el azar de un set)
    assert segs == ["interno", "marketing"]


def test_scored_rows_ahora_recorta_por_ambiente():
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos", ambiente="agente")
    query, params = cur.executed[0]
    assert "cs.segment = ANY(%(amb_segments)s)" in query
    assert params["amb_segments"] == ["agente"]


def test_pendientes_tambien_respeta_el_ambiente():
    # "Backfill en curso" es un numero que el tablero muestra al lado de los KPIs. Si
    # ignora el switch, dice 112.187 pendientes mires jugador, agente o sin_clasificar:
    # justo el numero-sin-origen que el ambiente viene a eliminar.
    cur = _CursorSecuencia([("q1", "Jugadores"), ("q2", "Agente 👨👩")], [(99,)])
    assert pending_sessions_count(cur, "sistemas", ambiente="agente") == 99
    query, params = cur.executed[-1]
    assert "JOIN conversations c ON c.id = cs.session_id" in query
    assert params["qids"] == ["q2"]


def test_pendientes_en_todos_NO_paga_el_join():
    # 'todos' es el default y el caso mas frecuente: sin recorte no hace falta el join.
    cur = _FakeCursor([(112187,)], one=(112187,))
    n = pending_sessions_count(cur, "sistemas", ambiente="todos")
    query, params = cur.executed[0]
    assert "JOIN conversations" not in query
    assert "qids" not in params
    assert n == 112187


def test_pendientes_sin_colas_del_ambiente_es_cero_sin_consultar():
    cur = _CursorSecuencia([("q1", "Agente 👨👩")])
    assert pending_sessions_count(cur, "sistemas", ambiente="jugador") == 0
    assert len(cur.executed) == 1


# --- el operador "sin identificar" de los cuadros --------------------------------
# Medido el 2026-08-07: en `sistemas` hay 29 operadores en `users` (729.683 mensajes) y
# 38 que NO estan en `users` (502.766 mensajes, el 40,8%). Los cuadros resolvian el
# nombre SOLO con u.name, sin fallback, asi que esas 38 personas se fusionaban en UNA
# fila llamada "Operador sin identificar": un operador ficticio gigante en la carga y un
# promedio sobre 38 personas en el % de deposito.
# El camino de scores y el modal ya tenian el fallback (COALESCE(u.name, cs.user_name)),
# pero los cuadros leen conversations/messages, donde esa columna no existe -> hay que
# reconstruir la firma '*Nombre:*' en SQL, igual que src/operators.build_operator_map.
# Medido: 34 de los 38 son rescatables por firma; 4 quedan anonimos de verdad.

def test_los_cuadros_resuelven_el_nombre_por_FIRMA_antes_de_rendirse():
    # La firma YA NO se reconstruye en SQL: llega resuelta y canonicalizada desde Python
    # (ver test_los_cuadros_reciben_el_mapa_de_identidad_ya_resuelto). Lo que este test
    # sigue fijando es el ORDEN: el nombre resuelto le gana al fallback.
    for sql in (_LOAD_SQL, _DEP_PCT_SQL):
        pos_sig = sql.index("sig.name")
        pos_fallback = sql.index("'Operador sin identificar'")
        assert pos_sig < pos_fallback, "la firma tiene que ganarle al fallback"


def test_los_cuadros_juntan_la_firma_por_user_id():
    for sql in (_LOAD_SQL, _DEP_PCT_SQL):
        assert "op_sig" in sql
        assert "sig.user_id = co.user_id" in sql


def test_el_fallback_sigue_existiendo_para_los_verdaderamente_anonimos():
    # 4 de los 38 no firman NUNCA: no se los puede nombrar y tienen que seguir visibles.
    for sql in (_LOAD_SQL, _DEP_PCT_SQL):
        assert "'Operador sin identificar'" in sql


def test_la_baja_logica_de_los_cuadros_usa_el_MISMO_nombre_resuelto():
    # Este era el agujero de verdad: el modal apaga por el nombre REAL (que si resuelve
    # por firma), pero los cuadros comparaban contra 'Operador sin identificar'. Para
    # esos 38 operadores la baja logica NO funcionaba en los cuadros.
    assert "sig.name" in _SIN_APAGADOS_CHARTS


def test_la_lista_trae_el_abandono_del_cliente():
    # Medido el 2026-08-07: el abandono ocurre en el 24,7% de las sesiones. Sin traerlo a la
    # LISTA hay que abrir sesion por sesion para saber por que un tramite quedo abierto, y
    # es justo el dato que explica que la nota NO sea culpa del operador.
    # Booleano derivado del jsonb: no necesita migracion ni engorda el payload.
    assert "cliente_abandono" in _TICKETS_CONVS_SQL
    assert "AS cliente_abandono" in _TICKETS_CONVS_SQL


def test_el_transcript_lleva_la_hora_de_cada_mensaje():
    # Sin la hora no se puede ver de un vistazo si alguien se demoro: el chat es donde se
    # entiende la demora, y hasta ahora el modal mostraba los mensajes sin reloj.
    from src.queries import _transcript
    from datetime import datetime, timezone
    t = datetime(2026, 8, 7, 20, 30, tzinfo=timezone.utc)
    out = _transcript([{"from_me": False, "is_note": False, "body": "hola",
                        "created_at": t, "media_type": "chat"}])
    assert out[0]["at"] == t.isoformat()


def test_el_transcript_tolera_mensajes_sin_hora():
    # El path por-conversacion de scripts/ no siempre trae created_at: no puede explotar.
    from src.queries import _transcript
    out = _transcript([{"from_me": True, "is_note": False, "body": "listo"}])
    assert out[0]["at"] is None


def _msg(minuto, from_me, body="hola", nota=False):
    from datetime import datetime, timezone
    return {"from_me": from_me, "is_note": nota, "body": body, "media_type": "chat",
            "created_at": datetime(2026, 8, 12, 10, minuto, tzinfo=timezone.utc)}


def test_el_transcript_numera_la_interaccion_de_cada_mensaje():
    # El modal mostraba la sesion entera como un solo chat. En el 10,2% de las de `jugador`
    # eso son VARIAS atenciones seguidas -- una de ellas de hace 51 horas -- y quien lee la
    # nota no tiene forma de saber donde termina una y arranca la otra.
    from src.queries import _transcript
    msgs = [_msg(0, False, "quiero depositar"), _msg(1, True, "dale"),
            _msg(2, True, "Ana *resuelto* la conversación", nota=True),
            _msg(40, False, "hola de nuevo"), _msg(41, True, "te ayudo")]
    out = _transcript(msgs)
    assert [m["interaccion"] for m in out] == [1, 1, 2, 2], "no numera por interaccion"
    assert all(m["interacciones"] == 2 for m in out), "falta el total, no hay contra que leer"


def test_una_sesion_de_una_sola_interaccion_no_se_anota():
    # El 96,3% de las sesiones son UNA interaccion. Ahi el separador seria ruido: no hay dos
    # tramos que distinguir, y marcar "1 de 1" en cada mensaje es peor que no decir nada.
    from src.queries import _transcript
    out = _transcript([_msg(0, False), _msg(1, True)])
    assert all(m["interacciones"] == 1 for m in out)
    assert all(m["juzgada"] for m in out), "con una sola, la juzgada es esa"


def test_el_transcript_marca_CUAL_interaccion_se_califico():
    # La nota describe UNA interaccion, no la sesion. Sin marcarla, el que audita lee una
    # calificacion de 2 estrellas al lado de un tramo que salio bien y concluye que el
    # sistema se equivoco -- que es exactamente lo que paso en la revision del 2026-08-12.
    # El inicio de la ventana juzgada es `conversation_created_at` de la fila: el worker lo
    # sobreescribe con el arranque de esa interaccion (ver src/worker.py), asi que no hace
    # falta guardar nada nuevo.
    from datetime import datetime, timezone

    from src.queries import _transcript
    msgs = [_msg(0, False, "quiero depositar"), _msg(1, True, "dale"),
            _msg(2, True, "Ana *resuelto* la conversación", nota=True),
            _msg(40, False, "hola de nuevo"), _msg(41, True, "te ayudo")]
    juzgada_desde = datetime(2026, 8, 12, 10, 40, tzinfo=timezone.utc)
    out = _transcript(msgs, juzgada_desde=juzgada_desde)
    assert [m["juzgada"] for m in out] == [False, False, True, True]
    # Sin ventana (el fall-through al LLM, que lee la sesion COMPLETA) se juzga todo: marcar
    # una sola seria decidir por el negocio cual representa la nota.
    assert all(m["juzgada"] for m in _transcript(msgs))


def test_las_opciones_de_los_desplegables_respetan_el_ambiente():
    # AUDITADO el 2026-08-07: `filter_options` ofrecia los 7 motivos incluso en `agente`,
    # donde las sesiones se califican con agilidad y el motivo es NULL. Elegir "Depósito"
    # ahi devolvia CERO filas sin ninguna explicacion: el desplegable prometia algo que el
    # ambiente no puede dar.
    cur = _FakeCursor([], description=[])
    filter_options(cur, "sistemas", ambiente="agente")
    for query, params in cur.executed:
        assert "cs.segment = ANY(%(amb_segments)s)" in query, query[:90]
        assert params["amb_segments"] == ["agente"]


def test_en_todos_las_opciones_no_se_recortan():
    cur = _FakeCursor([], description=[])
    filter_options(cur, "sistemas")
    for query, params in cur.executed:
        assert "amb_segments" not in (params or {})


def test_el_detalle_trae_el_ORIGEN_de_la_conversacion():
    # "Otros" junta 4 segmentos y esta dominado por la cola sin asignar (96,1%). Sin ver de
    # donde viene cada conversacion no se puede distinguir marketing de la cola vacia — que
    # es justo lo que hay que triajar.
    assert "cs.queue_name" in _DETAIL_SQL
    assert "cs.segment" in _DETAIL_SQL


def test_los_CTE_de_los_cuadros_son_MATERIALIZED():
    """Sin MATERIALIZED, /api/charts devolvia 500 en la cuenta `datos`.

    MEDIDO el 2026-08-07: el planner estimaba `conv_op` en 200 filas cuando son ~17.000, y
    con esa subestimacion elegia Nested Loops en cascada. Resultado en `datos`:

        actual        FALLO tras 90s   (el endpoint cortaba en el statement_timeout de 20s)
        MATERIALIZED     0,2s

    En `sistemas` no cambia nada (3,3 -> 3,4s, ruido). MATERIALIZED fuerza a calcular el CTE
    una vez en vez de inlinearlo y volver a estimarlo mal. `conv_dep` y `per_conv` ya lo
    usaban por la misma razon; a `msg_op`/`conv_op`/`op_sig` les faltaba.
    Este test existe para que nadie lo saque pensando que es decorativo.
    """
    for nombre, sql in (("_LOAD_SQL", _LOAD_SQL), ("_DEP_PCT_SQL", _DEP_PCT_SQL)):
        for cte in ("msg_op", "conv_op"):
            assert f"{cte} AS MATERIALIZED (" in sql, f"{nombre}: {cte} sin MATERIALIZED"
    from src.queries import _OP_SIG_CTE
    assert _OP_SIG_CTE.startswith("op_sig AS MATERIALIZED (")


def test_calidad_por_motivo_EXCLUYE_sin_motivo():
    """`sin_motivo` no es un motivo: es la ausencia de uno.

    Son las sesiones del segmento AGENTE, que se califican con la rubrica de agilidad y no
    pasan por la clasificacion de motivo (motivo NULL -> 'sin_motivo'). En una tarjeta que
    compara la calidad ENTRE motivos, meterlas es comparar peras con la falta de peras: en
    `sistemas` son el grupo mas grande y arrastraban el promedio del cuadro.
    Decision del negocio, 2026-08-07.
    """
    assert "cs.motivo IS NOT NULL" in _QUALITY_MOTIVO_SQL


def test_el_promedio_del_cuadro_no_mezcla_la_ausencia_de_motivo():
    # El builder tampoco debe generar la fila: si el SQL cambia, esto sigue protegiendo.
    filas = [("2026-07", "deposito", "Ana", 10, 40.0),
             ("2026-07", "sin_motivo", "Ana", 90, 450.0)]
    out = _build_quality_motivo(filas)
    assert "sin_motivo" not in [m["motivo"] for m in out["motivos"]]


def test_los_cuadros_reciben_el_mapa_de_identidad_ya_resuelto():
    """El nombre canonico se calcula UNA vez en Python y se inyecta, no se re-deriva en SQL.

    Dos razones. CORRECCION: la canonicalizacion por persona (unificar los user_id que el
    CRM recreo, sacando tildes y eligiendo la grafia dominante) no se puede hacer en SQL
    plano, y sin ella una persona con dos ids sigue apareciendo como dos operadores — y
    apagarla en la configuracion no la apaga entera.
    COSTO: el CTE con regex se calculaba en las TRES queries de cada request.
    """
    cur = _CursorSecuencia([("q1", "Jugadores")], [])
    load_by_operator(cur, "sistemas", op_map={"u1": "Anahí"})
    query, params = cur.executed[-1]
    assert "regexp_match" not in query, "no debe re-derivar la firma en SQL"
    assert "unnest(%(sig_ids)s" in query
    assert params["sig_ids"] == ["u1"] and params["sig_names"] == ["Anahí"]


def test_sin_mapa_los_cuadros_siguen_andando():
    # Compatibilidad: quien no pasa el mapa cae al nombre de `users` y al fallback.
    cur = _CursorSecuencia([("q1", "Jugadores")], [])
    load_by_operator(cur, "sistemas")
    query, params = cur.executed[-1]
    assert params["sig_ids"] == [] and params["sig_names"] == []
    assert "'Operador sin identificar'" in query


# La normalizacion de acentos de `_clave_sql` tenia las dos cadenas de translate con LARGOS
# DISTINTOS (23 contra 24). Postgres no se queja: ignora el sobrante y DESPLAZA el mapeo
# desde el caracter 16 en adelante, asi que `ñ` terminaba en 'a' en vez de 'n' y
# `Muñoz` NO matcheaba con `Munoz` — exactamente lo que la funcion existe para resolver.
# Hallado el 2026-08-12 armando el roster. Hoy no hay ni un nombre con ñ en `users`,
# `conversation_scores.user_name` ni `operator_status`, asi que estaba LATENTE: le pega al
# primer Muñoz/Peña/Nuñez que entre a trabajar.
# La mitad MAYUSCULA del mapeo era codigo muerto: `_clave_sql` aplica `lower()` antes de
# `translate`, asi que ninguna mayuscula acentuada llega nunca.

def test_clave_sql_normaliza_la_enie():
    from src.queries import _clave_sql
    sql = _clave_sql("x")
    desde = sql.split("'")[1]
    hacia = sql.split("'")[3]
    assert len(desde) == len(hacia), f"translate desalineado: {len(desde)} vs {len(hacia)}"
    assert hacia[desde.index("ñ")] == "n"


def test_clave_sql_no_arrastra_mayusculas_acentuadas():
    # Codigo muerto: el lower() va antes. Si estan, tienen que estar BIEN alineadas.
    from src.queries import _clave_sql
    sql = _clave_sql("x")
    desde = sql.split("'")[1]
    hacia = sql.split("'")[3]
    for i, c in enumerate(desde):
        assert hacia[i].islower() or not c.isalpha(), (
            f"{c!r} -> {hacia[i]!r}: el lower() ya paso, no deberia haber mayusculas")


# --- DOS CAUSAS, DOS ETIQUETAS -----------------------------------------------------
# 'Operador sin identificar' colapsaba DOS cosas distintas, y eso es peligroso cuando la
# etiqueta se puede APAGAR: si un bug futuro rompe la atribucion de alguien ACTIVO, su
# trabajo caeria en el mismo cajon apagado y desapareceria sin que nadie se entere.
# MEDIDO el 2026-08-12 sobre 130.558 filas evaluadas:
#   - 128 tienen `user_id` PERO no hay fila en `users` -> el CRM BORRO al usuario. Causa
#     conocida, historica (ene/feb/may 2026), no se puede recuperar el nombre: se APAGA.
#   - 675 no tienen NI `user_id` NI firma -> nosotros no lo pudimos atribuir. De esas, **640
#     tienen mensajes de un humano**: trabajo real sin nombre. Esa tiene que quedar VISIBLE.
#   - las otras 35 son solo-bot y estan bien excluidas (no hubo operador humano).

def test_operador_resuelto_separa_borrado_de_no_atribuido():
    from src.queries import _OPERADOR_RESUELTO
    assert "Operador borrado por Whaticket" in _OPERADOR_RESUELTO
    assert "Operador sin identificar" in _OPERADOR_RESUELTO
    # La causa se distingue por el JOIN al catalogo: user_id que NO resuelve = borrado.
    assert "u.id IS NULL" in _OPERADOR_RESUELTO
    assert "cs.user_id IS NOT NULL" in _OPERADOR_RESUELTO


def test_todas_las_consultas_por_operador_usan_LA_MISMA_expresion():
    # La expresion estaba INLINE y duplicada en 5 consultas, con `_OPERADOR_RESUELTO`
    # usado solo por la lista negra. Cinco copias de una regla de identidad es cinco
    # lugares donde puede divergir: si una dice 'borrado' y otra 'sin identificar', el
    # apagado se aplica en un cuadro y no en el otro.
    from src.queries import (_OPERADOR_RESUELTO, _OPS_MOTIVO_SQL, _OPS_SQL,
                             _QUALITY_MOTIVO_SQL, _QUALITY_SQL)
    for nombre, sql in (("_OPS_SQL", _OPS_SQL), ("_OPS_MOTIVO_SQL", _OPS_MOTIVO_SQL),
                        ("_QUALITY_SQL", _QUALITY_SQL),
                        ("_QUALITY_MOTIVO_SQL", _QUALITY_MOTIVO_SQL)):
        assert _OPERADOR_RESUELTO in sql, f"{nombre} no usa _OPERADOR_RESUELTO"


def test_el_cuadro_de_operadores_no_esconde_el_trabajo_humano_sin_atribuir():
    # El guard viejo era (u.name OR user_name OR user_id): una fila sin NINGUNO de los tres
    # se caia del cuadro en silencio, y son 640 sesiones con mensajes de un humano. Ahora
    # entra tambien por `agent_message_count > 0` -> el fallo se VE. El solo-bot sigue afuera.
    from src.queries import _OPS_SQL
    assert "cs.agent_message_count > 0" in _OPS_SQL


# --- LA REGLA DE IDENTIDAD VIVE EN UN SOLO LUGAR ------------------------------------
# El 2026-08-12 se unificaron las 5 consultas de `queries.py`... y quedaron TRES copias mas
# en `operators_status.py` (el modal de prender/apagar) y una en el front. Resultado: el
# usuario seguia viendo "Operador sin identificar" en el modal, que ademas NO conocia el
# split de `Operador borrado por Whaticket` y NO filtraba `eval_status`, asi que 4 filas
# SALTEADAS sin un solo mensaje del negocio creaban un operador fantasma.
# Y peor: `operators_status.py` tenia su PROPIA copia del translate de acentos con el bug de
# la ñ (23 caracteres contra 24) que ya se habia arreglado en `queries.py`.
# La regla ahora vive en `src/identidad.py`, igual que el horario vive en `src/horario.py`.

def test_la_etiqueta_de_identidad_solo_se_define_en_identidad_py():
    # Mira las lineas de CODIGO, no los comentarios: la historia de por que esta regla
    # divergio esta escrita en prosa en varios archivos y esa prosa no es una copia.
    from pathlib import Path
    culpables = []
    for f in Path("src").glob("*.py"):
        if f.name == "identidad.py":
            continue
        for n, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if linea.strip().startswith("#"):
                continue
            if "'Operador sin identificar'" in linea:
                culpables.append(f"{f.name}:{n}")
    assert not culpables, f"la etiqueta se define fuera de identidad.py: {culpables}"


def test_el_translate_de_acentos_solo_se_define_en_identidad_py():
    from pathlib import Path
    culpables = []
    for f in Path("src").glob("*.py"):
        if f.name == "identidad.py":
            continue
        if "aeiouuaeiouaeion" in f.read_text(encoding="utf-8"):
            culpables.append(f.name)
    assert not culpables, f"el translate se define fuera de identidad.py: {culpables}"


def test_el_filtro_de_operador_compara_contra_LO_QUE_OFRECE_el_desplegable():
    # El desplegable ofrece la etiqueta RESUELTA (con el split y la cuarta puerta) y el filtro
    # comparaba el `COALESCE(u.name, cs.user_name)` crudo -- que es NULL justo en las dos
    # etiquetas. Elegir "Operador borrado por Whaticket" devolvia CERO filas sin explicacion:
    # exactamente lo que el docstring de `filter_options` dice no volver a hacer.
    import inspect

    from src.identidad import OPERADOR_RESUELTO
    from src.queries import _scores_filters, filter_options
    ofrece = inspect.getsource(filter_options)
    filtra = inspect.getsource(_scores_filters)
    assert OPERADOR_RESUELTO in ofrece or "_OPERADOR_RESUELTO" in ofrece
    assert "_OPERADOR_RESUELTO" in filtra, "el filtro no usa la expresion resuelta"
    assert "COALESCE(u.name, cs.user_name) = %(op)s" not in filtra


def test_el_front_NO_calcula_la_etiqueta_de_identidad():
    # La regla vive en SQL. Si el front la re-deriva desde `user_name`/`user_id`, no conoce el
    # split de "borrado por Whaticket" ni la cuarta puerta, y dice otra cosa que los cuadros.
    # Nombrarla para EXPLICARLA si vale (el mapa de tips): las dos etiquetas significan cosas
    # distintas y una es un fallo nuestro. Lo prohibido es DERIVARLA.
    from pathlib import Path

    from src.identidad import BORRADO_POR_CRM, SIN_IDENTIFICAR
    html = Path("web/index.html").read_text(encoding="utf-8")
    linea_opname = next(ln for ln in html.splitlines() if ln.startswith("const opName ="))
    assert SIN_IDENTIFICAR not in linea_opname, "el front re-deriva la etiqueta"
    assert "user_id" not in linea_opname, "el front decide por user_id, como hacia el SQL"
    for etiqueta in (SIN_IDENTIFICAR, BORRADO_POR_CRM):
        usos = [ln.strip() for ln in html.splitlines() if etiqueta in ln]
        assert usos, f"{etiqueta} no esta explicada en el front"
        for u in usos:
            assert u.startswith(f'"{etiqueta}":'), f"uso que no es un tip: {u[:80]}"


def test_el_modal_y_los_cuadros_listan_el_MISMO_universo():
    # Si el modal lista operadores que los cuadros no muestran, el que prende/apaga esta
    # decidiendo sobre filas que no existen en ningun promedio.
    from src.identidad import OPERADOR_RESUELTO
    from src.operators_status import _ACTIVITY, _ADMIN_ROWS
    from src.queries import _OPS_SQL
    for sql in (_ADMIN_ROWS, _OPS_SQL):
        assert "cs.agent_message_count > 0" in sql, "falta la cuarta puerta"
    # La etiqueta SI aparece en el SQL armado -- la expresion la embebe. Lo que se exige es
    # que sea la MISMA expresion, no una tipeada al lado.
    for sql in (_ADMIN_ROWS, _ACTIVITY, _OPS_SQL):
        assert OPERADOR_RESUELTO in sql, "no usa la expresion de identidad.py"
        # `{where}` y `{cola}` son placeholders LEGITIMOS de .format(). Los de identidad no:
        # `operators_status.py` quedo con las constantes en un string sin la `f`, asi que el
        # SQL viajaba con `{OPERADOR_RESUELTO}` como texto y Postgres reventaba.
        for ph in ("{OPERADOR_RESUELTO}", "{HAY_OPERADOR}", "{clave_sql", "{clave_os}"):
            assert ph not in sql, f"placeholder de identidad sin interpolar: {ph}"


# --- NINGUN SQL PUEDE LLEVAR UN COMENTARIO DE PYTHON ---------------------------------
# El 2026-08-12, al partir el arbol en commits, un comentario de Python quedo DENTRO del
# string de `_CONV_BY_MONTH_SQL`. Postgres respondio `syntax error at or near "#"` y la
# tarjeta de conversion por mes devolvia 500 -- y ademas abortaba la transaccion, asi que
# TODO lo que venia despues en el mismo request moria con `InFailedSqlTransaction`.
# Los tests de esas consultas miran el TEXTO (que contengan tal columna) y ninguno la
# EJECUTA, asi que la suite entera seguia en verde. En SQL el comentario es `--`.

def test_ningun_sql_lleva_comentarios_de_python():
    import re

    import src.queries as q
    culpables = []
    for nombre in dir(q):
        if not nombre.endswith("_SQL"):
            continue
        sql = getattr(q, nombre)
        if not isinstance(sql, str):
            continue
        for n, linea in enumerate(sql.splitlines(), 1):
            # `#` al inicio de la linea: en SQL no existe. Dentro de un literal si podria
            # aparecer (un `'#tag'`), asi que se exige que ARRANQUE la linea.
            if re.match(r"\s*#", linea):
                culpables.append(f"{nombre}:{n}: {linea.strip()[:70]}")
    assert not culpables, "comentario de Python dentro de un SQL:\n  " + "\n  ".join(culpables)


# --- DESGLOSE DE "SIN EVALUAR" POR CAUSA -------------------------------------------
# El tablero mostraba "sin evaluar" como UN numero (`summary_kpis.no_evaluadas`) y no habia
# forma de saber por que. Es el mismo problema que ya arreglo `_MOTIVO_STATS_SQL` para el
# promedio por motivo -- ahi el docstring lo dice textual: "Era todo lo salteado en una bolsa
# con el nombre de una sola de sus causas". Esta tarjeta le pone nombre a cada causa.
#
# Lo pidio el negocio el 2026-08-13 despues de descubrir que `redireccion` no se veia en el
# tablero: la fila existia, pero para contar cuantas eran habia que filtrar la lista a ojo.

def test_build_skip_stats_ordena_por_volumen_y_saca_porcentaje():
    from src.queries import _build_skip_stats
    out = _build_skip_stats([("sin_motivo", 560, 0), ("no_agent_reply", 313, 102),
                             ("customer_media_only", 228, 0)])
    assert [r["skip_reason"] for r in out] == [
        "sin_motivo", "no_agent_reply", "customer_media_only"]
    assert out[0]["n"] == 560
    assert sum(r["n"] for r in out) == 1101
    # el porcentaje es sobre EL TOTAL SALTEADO, no sobre la poblacion entera: la pregunta
    # de la tarjeta es "de lo que no se evaluo, cuanto es cada cosa".
    assert out[0]["pct"] == round(100 * 560 / 1101, 1)


def test_build_skip_stats_con_cero_filas_no_divide_por_cero():
    from src.queries import _build_skip_stats
    assert _build_skip_stats([]) == []


def test_build_skip_stats_nombra_la_causa_faltante_en_vez_de_perderla():
    # Una fila `skipped` sin `skip_reason` es un bug del worker, pero desaparecerla del
    # desglose lo esconde: el total dejaria de cerrar contra el KPI.
    from src.queries import _build_skip_stats
    out = _build_skip_stats([("sin_motivo", 5, 0), (None, 2, 0)])
    claves = [r["skip_reason"] for r in out]
    assert "sin_causa" in claves, claves
    assert sum(r["n"] for r in out) == 7


def test_el_desglose_cuenta_LA_MISMA_poblacion_que_el_KPI_de_sin_evaluar():
    # El KPI usa `eval_status <> 'evaluated'`; el desglose tiene que usar EXACTAMENTE eso,
    # o la tarjeta suma distinto que el numero que esta arriba de ella.
    from src.queries import _SKIP_STATS_SQL, _SUMMARY_KPIS_SQL
    assert "eval_status <> 'evaluated'" in _SKIP_STATS_SQL
    assert "eval_status <> 'evaluated'" in _SUMMARY_KPIS_SQL


def test_skip_stats_esta_en_el_summary():
    import inspect
    from src.queries import summary
    assert "skip_stats" in inspect.getsource(summary)


# --- LA ALERTA DEL JUGADOR SIN RESPUESTA -------------------------------------------
# Pedida por el negocio el 2026-08-13: "si son internos está bien, pero si es de canal de
# jugador sí quiero que haya una alerta ahí".
#
# LO QUE LO MOTIVO, medido: de las 313 sesiones `no_agent_reply`, **160 son GRUPOS de
# WhatsApp** (segmento `interno`, numero de 18 digitos tipo `120363433857149469`, 129
# mensajes de media de gente charlando entre si) y ahi nadie del negocio tiene que contestar.
# Pero **102 son del segmento `jugador`** -- 50 en `Jugadores 🍀`, 32 en `OnlySorti`, 13 en
# `ModoSorti`, 7 en `sortiGO` --, con 1 o 2 mensajes, CERO grupos y 7 a 12 dias de antiguedad.
# Son personas que escribieron y nadie les contesto nunca. Ese numero no puede estar
# escondido dentro del mismo renglon que los grupos.
#
# OJO CON EL FALLBACK DE SEGMENTO: `segment_for_queue` devuelve 'interno' cuando la cola es
# NULL o vacia (src/segments.py:51-52), asi que 'interno' NO es una clasificacion positiva,
# es "sin cola". Por eso la alerta se cuelga de `jugador`, que SI se afirma por nombre de
# cola, y no de "no es interno".

def test_build_skip_stats_separa_las_de_jugador():
    from src.queries import _build_skip_stats
    out = _build_skip_stats([("no_agent_reply", 313, 102), ("sin_motivo", 560, 0)])
    por_causa = {r["skip_reason"]: r for r in out}
    assert por_causa["no_agent_reply"]["jugador"] == 102
    assert por_causa["sin_motivo"]["jugador"] == 0


def test_el_desglose_cuenta_jugador_por_SEGMENTO_no_por_cuenta():
    # `jugador` vive en las DOS cuentas (50 en sistemas + 52 en datos): contar por cuenta
    # partiria la alerta en dos y ninguna de las dos mitades se veria grave.
    from src.queries import _SKIP_STATS_SQL
    assert "cs.segment = 'jugador'" in _SKIP_STATS_SQL


def test_build_skip_stats_nunca_reporta_mas_jugadores_que_sesiones():
    from src.queries import _build_skip_stats
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _build_skip_stats([("no_agent_reply", 10, 11)])
