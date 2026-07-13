"""Tests de los endpoints de agregación (B2). Los endpoints son glue fino: mapean
los query params (incl. alias from/to) a los filtros y llaman al query layer (ya
probado en test_queries). Se mockea la conexión y el query layer para no tocar BD."""
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


def test_robots_txt_no_indexar():
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /" in r.text


def test_options_endpoint(monkeypatch):
    calls = _stub(monkeypatch, "filter_options")
    r = client.get("/api/options", params={"account": "datos"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls["account"] == "datos"
