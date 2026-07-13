"""API FastAPI + dashboard (un solo contenedor, account-scoped).

Sirve el dashboard en `/` y lee la BD en vivo bajo `/api/*`. Toda lectura de
scores exige `account`: datos y sistemas conviven en la misma base y el front
elige cual traer. Config por entorno (EasyPanel). Ver src/config.py.
"""
from __future__ import annotations

import logging
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


# Índices que el dashboard NECESITA para que /api/charts no degenere en seq scans
# de messages (2M filas). Viven en la BD, no en el repo -> se aseguran al arrancar
# para que un entorno nuevo (p. ej. la BD de producción) se autocure sin tocar la
# base a mano. Idempotente por IF NOT EXISTS.
_REQUIRED_INDEXES = (
    (
        "idx_messages_account_conv",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_account_conv "
        "ON messages (account, conversation_id)",
    ),
)


def ensure_indexes() -> None:
    """Asegura los índices del dashboard si faltan (idempotente, falla suave).

    CONCURRENTLY no bloquea escrituras del ETL y NO puede correr dentro de una
    transacción -> conexión propia en autocommit, SIN el statement_timeout del
    API (el build inicial puede tardar más que ese ceiling). Si algo falla, se
    loguea y el app arranca igual: servirá, sólo más lento hasta que el índice
    exista."""
    # "uvicorn.error" es el logger que uvicorn engancha a stdout -> se ve en los
    # logs del contenedor (EasyPanel), que es donde vas a confirmar el build.
    log = logging.getLogger("uvicorn.error")
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8, autocommit=True) as c:
            for name, ddl in _REQUIRED_INDEXES:
                try:
                    c.execute(ddl)
                    log.info("índice asegurado: %s", name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("no se pudo asegurar el índice %s: %s", name, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_indexes: sin conexión a la BD (%s); se omite", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Arranca el worker de scoring en el mismo contenedor si esta habilitado."""
    # En un thread aparte: el build CONCURRENTLY puede tardar segundos y no debe
    # bloquear el arranque ni el event loop. Mientras tanto el API responde (más
    # lento, con statement_timeout como red de seguridad).
    threading.Thread(target=ensure_indexes, daemon=True, name="ensure-indexes").start()
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
    # plan_cache_mode=force_custom_plan: los cuadros filtran `account = $1`, que
    # tiene 2 valores (datos/sistemas). Con plan genérico Postgres estima ~50% de
    # las filas y elige seq scan de los 2M mensajes, ignorando idx_messages_account_conv;
    # para "datos" (2,4% de la tabla) eso es CPU-bound de decenas de segundos. El plan
    # custom re-planifica con el valor real -> index scan (~100ms).
    # statement_timeout: una query colgada NO se cancela cuando el cliente hace timeout
    # (queda huérfana escaneando 2M filas y se apilan). El ceiling la mata en el server.
    return psycopg.connect(
        cfg.database_url,
        connect_timeout=8,
        options="-c plan_cache_mode=force_custom_plan -c statement_timeout=20000",
    )


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
    """Agregados FULL-SCALE para los cuadros del análisis (deterministas, sobre el
    segmento jugador; no dependen del scoring LLM): carga por operador, % depósito
    en WhatsApp por operador y nuevos jugadores vs % depósito por mes."""
    win = cfg.charts_window_months
    with _conn() as c, c.cursor() as cur:
        return {
            "load_by_operator": queries.load_by_operator(cur, account, window_months=win),
            "deposit_pct_by_operator": queries.deposit_pct_by_operator(cur, account, window_months=win),
            "new_vs_deposit_by_month": queries.new_vs_deposit_by_month(cur, account, window_months=win),
            "window_months": win,
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
