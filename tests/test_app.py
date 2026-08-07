"""Tests de los endpoints de agregación (B2). Los endpoints son glue fino: mapean
los query params (incl. alias from/to) a los filtros y llaman al query layer (ya
probado en test_queries). Se mockea la conexión y el query layer para no tocar BD."""
import dataclasses

import src.app as appmod
from fastapi.testclient import TestClient

client = TestClient(appmod.app)


class _DummyCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _DummyCtx()

    # Los endpoints de ESCRITURA commitean; los de lectura no. El doble tiene que
    # soportar las dos formas sin fingir que la escritura pasó por una BD real.
    def commit(self):
        return None

    def rollback(self):
        return None


def _stub(monkeypatch, name):
    """Reemplaza queries.<name> por una captura de (account, kwargs)."""
    calls = {}

    def fake(cur, account, **kwargs):
        calls["account"] = account
        calls["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    monkeypatch.setattr(appmod.queries, name, fake)
    return calls


def test_summary_endpoint_mapea_filtros(monkeypatch):
    calls = _stub(monkeypatch, "summary")
    r = client.get("/api/summary", params={
        "account": "datos", "segment": "jugador", "from": "2026-01-01",
        "to": "2026-06-30", "rating": "buena", "search": "juan"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls["account"] == "datos"
    k = calls["kwargs"]
    assert k["segment"] == "jugador"
    assert k["date_from"] == "2026-01-01" and k["date_to"] == "2026-06-30"  # alias from/to
    assert k["rating"] == "buena" and k["search"] == "juan"
    assert k["estado"] == "all" and k["canal"] == "all" and k["op"] == "all"  # defaults


def test_tickets_endpoint_mapea_page_sort_y_filtros(monkeypatch):
    calls = _stub(monkeypatch, "tickets_page")
    r = client.get("/api/tickets", params={
        "account": "sistemas", "page": 3, "sort": "best", "op": "Ana", "canal": "WHATSAPP"})
    assert r.status_code == 200
    k = calls["kwargs"]
    assert k["page"] == 3 and k["sort"] == "best"
    assert k["op"] == "Ana" and k["canal"] == "WHATSAPP"


def test_summary_endpoint_exige_account(monkeypatch):
    _stub(monkeypatch, "summary")
    assert client.get("/api/summary").status_code == 422  # account requerido


def test_conversion_endpoint_mapea_filtros_y_combina(monkeypatch):
    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    seen = {}
    monkeypatch.setattr(appmod.queries, "conversion_by_operator",
                        lambda cur, account, **k: seen.setdefault("op", (account, k)) or {"operators": []})
    monkeypatch.setattr(appmod.queries, "conversion_by_month",
                        lambda cur, account, **k: {"months": []})
    monkeypatch.setattr(appmod.queries, "conversion_passivity_evolution",
                        lambda cur, account, **k: {"months": [], "operators": []})
    r = client.get("/api/conversion", params={"account": "sistemas", "canal": "WHATSAPP", "from": "2026-01-01"})
    assert r.status_code == 200
    assert set(r.json()) == {"by_operator", "by_month", "evolution"}
    account, k = seen["op"]
    assert account == "sistemas" and k["canal"] == "WHATSAPP" and k["date_from"] == "2026-01-01"


def test_conversion_cohort_endpoint(monkeypatch):
    calls = {}

    def fake(cur, account, **kwargs):
        calls["account"] = account; calls["kwargs"] = kwargs
        return [{"contact_id": "c1", "deposited": True}]   # el endpoint devuelve lista

    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    monkeypatch.setattr(appmod.queries, "conversion_cohort", fake)
    r = client.get("/api/conversion/cohort", params={"account": "sistemas", "op": "Virginia"})
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert calls["account"] == "sistemas" and calls["kwargs"]["op"] == "Virginia"


# --- operadores: lectura abierta, ESCRITURA con token ------------------------

def _stub_ops(monkeypatch, rows=None):
    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    monkeypatch.setattr(appmod.operators_status, "ensure_table", lambda cur: None)
    monkeypatch.setattr(appmod.operators_status, "admin_rows",
                        lambda cur, account: rows if rows is not None else [])
    aplicados = []
    monkeypatch.setattr(appmod.operators_status, "set_many",
                        lambda cur, account, pares, updated_by="ui":
                            aplicados.append((account, pares, updated_by)) or len(pares))
    return aplicados


def test_get_operators_no_pide_token(monkeypatch):
    """La lectura expone lo mismo que los cuadros ya muestran (nombres y volumen), así que
    no se le pone una barrera que no protege nada."""
    _stub_ops(monkeypatch, rows=[{"operador": "Mel", "activo": True, "sesiones": 2840,
                                  "recientes": 1821, "ultima_actividad": "2026-08-04"}])
    r = client.get("/api/operators", params={"account": "sistemas"})
    assert r.status_code == 200
    assert r.json()["operadores"][0]["operador"] == "Mel"
    assert r.json()["umbral_sugerido"] == 100


def test_put_operators_SIN_token_configurado_queda_CERRADO(monkeypatch):
    """Falla CERRADA: si nadie configuró DASHBOARD_ADMIN_TOKEN, escribir es imposible.
    Al revés (abierto por default) un despliegue sin la variable dejaría a cualquiera
    apagando operadores sin que nadie se entere."""
    _stub_ops(monkeypatch)
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_token=""))
    r = client.put("/api/operators", json={"account": "sistemas", "operadores": []})
    assert r.status_code == 503
    assert "token" in r.json()["detail"].lower()


def test_put_operators_token_incorrecto_o_ausente(monkeypatch):
    _stub_ops(monkeypatch)
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_token="secreto-real"))
    body = {"account": "sistemas", "operadores": [{"operador": "Mel", "activo": False}]}
    assert client.put("/api/operators", json=body).status_code == 401
    assert client.put("/api/operators", json=body,
                      headers={"X-Admin-Token": "otro"}).status_code == 401


def test_put_operators_con_token_valido_aplica(monkeypatch):
    aplicados = _stub_ops(monkeypatch)
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_token="secreto-real"))
    body = {"account": "sistemas",
            "operadores": [{"operador": "MariCruz", "activo": False},
                           {"operador": "Mel", "activo": True}]}
    r = client.put("/api/operators", json=body, headers={"X-Admin-Token": "secreto-real"})
    assert r.status_code == 200 and r.json()["actualizados"] == 2
    account, pares, _ = aplicados[0]
    assert account == "sistemas"
    assert pares == [("MariCruz", False), ("Mel", True)]


def test_put_operators_rechaza_body_vacio_o_sin_cuenta(monkeypatch):
    _stub_ops(monkeypatch)
    monkeypatch.setattr(appmod, "cfg", dataclasses.replace(appmod.cfg, admin_token="t"))
    h = {"X-Admin-Token": "t"}
    assert client.put("/api/operators", json={"operadores": []}, headers=h).status_code == 422
    # nombre vacío: apagaría una fila fantasma
    r = client.put("/api/operators", headers=h,
                   json={"account": "x", "operadores": [{"operador": "  ", "activo": False}]})
    assert r.status_code == 422


def test_robots_txt_no_indexar():
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /" in r.text


def test_options_endpoint(monkeypatch):
    calls = _stub(monkeypatch, "filter_options")
    r = client.get("/api/options", params={"account": "datos"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls["account"] == "datos"


# --- AMBIENTE: el switch en las peticiones --------------------------------------

def test_summary_pasa_el_ambiente_al_query_layer(monkeypatch):
    calls = _stub(monkeypatch, "summary")
    r = client.get("/api/summary", params={"account": "datos", "ambiente": "agente"})
    assert r.status_code == 200
    assert calls["kwargs"]["ambiente"] == "agente"


def test_el_ambiente_por_default_es_todos(monkeypatch):
    # Sin el param, el tablero no esconde nada. El recorte es una decision explicita.
    calls = _stub(monkeypatch, "summary")
    client.get("/api/summary", params={"account": "datos"})
    assert calls["kwargs"]["ambiente"] == "todos"


def test_un_ambiente_inventado_es_422_no_un_tablero_entero(monkeypatch):
    # Degradar un typo a 'todos' mostraria TODAS las audiencias haciendole creer al que
    # mira que ve una sola. Se rechaza en el borde.
    _stub(monkeypatch, "summary")
    r = client.get("/api/summary", params={"account": "datos", "ambiente": "jugadores"})
    assert r.status_code == 422


def test_charts_pasa_el_ambiente_a_los_TRES_cuadros(monkeypatch):
    vistos = {}

    def fake(name):
        def f(cur, account, **kwargs):
            vistos[name] = kwargs
            return {"ok": True}
        return f

    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    for name in ("load_by_operator", "deposit_pct_by_operator", "new_vs_deposit_by_month"):
        monkeypatch.setattr(appmod.queries, name, fake(name))
    r = client.get("/api/charts", params={"account": "sistemas", "ambiente": "agente"})
    assert r.status_code == 200
    for name in ("load_by_operator", "deposit_pct_by_operator", "new_vs_deposit_by_month"):
        assert vistos[name]["ambiente"] == "agente", name


def test_charts_declara_el_ambiente_que_aplico(monkeypatch):
    # El origen del numero viaja CON el numero: sin esto el front rotula de memoria.
    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    for name in ("load_by_operator", "deposit_pct_by_operator", "new_vs_deposit_by_month"):
        monkeypatch.setattr(appmod.queries, name, lambda cur, account, **k: {"ok": True})
    r = client.get("/api/charts", params={"account": "sistemas", "ambiente": "sin_clasificar"})
    assert r.json()["ambiente"] == "sin_clasificar"


def test_charts_default_jugador_conserva_la_conducta_vieja(monkeypatch):
    vistos = {}
    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())

    def f(cur, account, **kwargs):
        vistos.update(kwargs)
        return {"ok": True}

    for name in ("load_by_operator", "deposit_pct_by_operator", "new_vs_deposit_by_month"):
        monkeypatch.setattr(appmod.queries, name, f)
    client.get("/api/charts", params={"account": "sistemas"})
    assert vistos["ambiente"] == "jugador"


def test_scores_ahora_acepta_los_filtros(monkeypatch):
    # Era el unico endpoint de lectura que ignoraba TODO filtro: traia la cuenta entera.
    # Stub propio: este endpoint declara list[dict], no dict.
    calls = {}

    def fake(cur, account, **kwargs):
        calls["account"] = account
        calls["kwargs"] = kwargs
        return [{"conversation_id": "c1"}]

    monkeypatch.setattr(appmod, "_conn", lambda: _DummyCtx())
    monkeypatch.setattr(appmod.queries, "scored_rows", fake)
    r = client.get("/api/scores", params={"account": "datos", "ambiente": "agente"})
    assert r.status_code == 200
    assert calls["kwargs"]["ambiente"] == "agente"


def test_endpoint_de_composicion_de_ambientes(monkeypatch):
    calls = _stub(monkeypatch, "ambiente_composition")
    r = client.get("/api/ambientes", params={"account": "sistemas"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls["account"] == "sistemas"
