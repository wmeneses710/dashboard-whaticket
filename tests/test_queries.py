"""Tests de la capa de queries: lo importante es que TODA lectura de scores
esta scopeada por cuenta (datos vs sistemas conviven en la misma BD)."""
from decimal import Decimal

from src.queries import (
    _build_dep_channel,
    _build_load_series,
    _build_new_vs_deposit,
    _build_ops,
    _build_pct_series,
    _dist_from_labels,
    _scores_filters,
    conversation_detail,
    deposit_by_channel,
    distribution,
    operators_table,
    scored_rows,
    summary,
    summary_kpis,
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
    # La firma '*Nombre:*' (cs.user_name) queda solo de fallback: COALESCE.
    cur = _FakeCursor([], description=[])
    scored_rows(cur, "datos")
    query, _ = cur.executed[0]
    assert "JOIN users" in query
    assert "COALESCE(u.name, cs.user_name) AS user_name" in query


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
    # campos de solo-detalle fuera de la lista (peso muerto)
    for dead in ("cs.queue_name", "cs.resolved_at", "cs.rubric", "cs.message_count",
                 "cs.agent_message_count", "cs.bot_message_count", "cs.contact_message_count",
                 "cs.first_response_seconds", "cs.resolution_seconds", "cs.was_unassigned"):
        assert dead not in query, f"{dead} deberia salir de la lista"
    # lo que la lista SI usa se mantiene
    for keep in ("cs.stars", "cs.rating_label", "cs.deposit_count", "cs.segment", "AS user_name"):
        assert keep in query, f"{keep} no deberia salir de la lista"


def test_scores_filters_base_solo_cuenta():
    where, params = _scores_filters("datos")
    assert where == "cs.account = %(account)s"
    assert params == {"account": "datos"}


def test_scores_filters_aplica_cada_filtro():
    where, params = _scores_filters(
        "sistemas", estado="evaluated", segment="jugador", canal="WHATSAPP",
        op="Virginia", date_from="2026-01-01", date_to="2026-06-30", search="juan")
    assert "cs.eval_status = %(estado)s" in where and params["estado"] == "evaluated"
    assert "cs.segment = %(segment)s" in where and params["segment"] == "jugador"
    assert "t.channel = %(canal)s" in where and params["canal"] == "WHATSAPP"
    assert "COALESCE(u.name, cs.user_name) = %(op)s" in where and params["op"] == "Virginia"
    assert "cs.conversation_created_at >= %(dfrom)s" in where and params["dfrom"] == "2026-01-01"
    assert "cs.conversation_created_at <= %(dto)s" in where and params["dto"] == "2026-06-30"
    # búsqueda: mismos campos que matchBase del front (cliente, número, operador)
    assert "ILIKE %(q)s" in where and params["q"] == "%juan%"


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
    assert "FILTER (WHERE cs.deposit_count > 0)" in query
    assert "GROUP BY 1" in query


def test_summary_combina_las_cuatro_secciones():
    cur = _FakeCursor(rows=[], description=["total", "evaluadas", "avg_stars", "depositos", "dep_conv", "operadores"],
                      one=(0, 0, None, 0, 0, 0))
    out = summary(cur, "datos")
    assert set(out) == {"kpis", "distribution", "operators", "deposit_by_channel"}


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
    rows = [("2026-02", 50, 10, 30), ("2026-01", 100, 42, 57)]
    out = _build_new_vs_deposit(rows)
    assert out["months"] == ["2026-01", "2026-02"]        # ordenado por mes
    assert out["nuevos"] == [57, 30]
    assert out["pct"] == [42.0, 20.0]                      # 42/100 y 10/50


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
