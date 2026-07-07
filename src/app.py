"""API FastAPI + dashboard (un solo contenedor, account-scoped).

Sirve el dashboard en `/` y lee la BD en vivo bajo `/api/*`. Toda lectura de
scores exige `account`: datos y sistemas conviven en la misma base y el front
elige cual traer. Config por entorno (EasyPanel). Ver src/config.py.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import queries
from src.config import load_config
from src.worker import run_worker_loop

cfg = load_config()
_WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"
_VENDOR = _WEB.parent / "vendor"  # libs estáticas (Chart.js), servidas local


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Arranca el worker de scoring en el mismo contenedor si esta habilitado."""
    stop = threading.Event()
    if cfg.scoring_enabled:
        thread = threading.Thread(
            target=run_worker_loop, args=(cfg,), kwargs={"should_stop": stop.is_set},
            daemon=True, name="scoring-worker",
        )
        thread.start()
    yield
    stop.set()


app = FastAPI(title="dashboard-whaticket", version="1.0", lifespan=lifespan)
app.mount("/vendor", StaticFiles(directory=str(_VENDOR)), name="vendor")


def _conn():
    return psycopg.connect(cfg.database_url, connect_timeout=8)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_WEB)


@app.get("/api/accounts")
def accounts() -> list[dict]:
    """Cuentas disponibles (con conteo) para el selector."""
    with _conn() as c, c.cursor() as cur:
        return queries.list_accounts(cur)


@app.get("/api/scores")
def scores(account: str = Query(..., description="datos | sistemas")) -> list[dict]:
    """Conversaciones scoreadas de una cuenta (sin transcript)."""
    with _conn() as c, c.cursor() as cur:
        return queries.scored_rows(cur, account)


@app.get("/api/charts")
def charts(account: str = Query(..., description="datos | sistemas")) -> dict:
    """Agregados FULL-SCALE para los cuadros (deterministas, sobre toda la cuenta;
    no dependen del scoring LLM). Hoy: depósitos por mes."""
    with _conn() as c, c.cursor() as cur:
        return {
            "deposits_by_month": queries.deposits_by_month(cur, account),
            "load_by_operator": queries.load_by_operator(cur, account),
            "deposit_pct_by_operator": queries.deposit_pct_by_operator(cur, account),
        }


@app.get("/api/conversation/{cid}")
def conversation(cid: str) -> dict:
    """Detalle completo de una conversacion + transcript (on-demand)."""
    with _conn() as c, c.cursor() as cur:
        detail = queries.conversation_detail(cur, cid)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversacion no encontrada")
    return detail


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
