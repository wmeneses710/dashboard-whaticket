"""API FastAPI + dashboard (un solo contenedor, account-scoped).

Sirve el dashboard en `/` y lee la BD en vivo bajo `/api/*`. Toda lectura de
scores exige `account`: datos y sistemas conviven en la misma base y el front
elige cual traer. Config por entorno (EasyPanel). Ver src/config.py.
"""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from src import operators_status, queries
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


def seed_operator_status() -> None:
    """Asegura `operator_status` y la SIEMBRA desde config/operadores.json (idempotente).

    Corre en cada arranque del contenedor. La siembra NO PISA (ON CONFLICT DO NOTHING): si
    alguien apagó un operador desde el modal en producción, ese cambio vive en la BD y un
    deploy no puede borrarlo. El archivo solo llena huecos — operadores nuevos, o una base
    recién restaurada. Para que el archivo gane hay que pedirlo a mano con
    `scripts/load_operadores.py --pisar`.

    Falla suave: si la BD no está o el archivo tiene un error, el dashboard arranca igual y
    simplemente no habrá nadie apagado (default = todos visibles, que es el lado seguro)."""
    log = logging.getLogger("uvicorn.error")
    try:
        operadores = operators_status.load_config()
        with psycopg.connect(cfg.database_url, connect_timeout=8) as c:
            with c.cursor() as cur:
                operators_status.ensure_table(cur)
                n = operators_status.seed_from_config(cur, operadores)
                apagados = {
                    cuenta: len(operators_status.inactive_names(cur, cuenta))
                    for cuenta in operadores.get("cuentas", {})
                }
            c.commit()
        log.info("operator_status: %s filas sembradas · apagados por cuenta: %s", n, apagados)
    except Exception as exc:  # noqa: BLE001
        log.warning("operator_status: no se pudo sembrar (%s); nadie queda apagado", exc)


def _bootstrap() -> None:
    """Tareas de arranque que no deben bloquear el event loop, en orden de importancia."""
    ensure_indexes()
    seed_operator_status()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Arranca el worker de scoring en el mismo contenedor si esta habilitado."""
    # En un thread aparte: el build CONCURRENTLY puede tardar segundos y no debe
    # bloquear el arranque ni el event loop. Mientras tanto el API responde (más
    # lento, con statement_timeout como red de seguridad).
    threading.Thread(target=_bootstrap, daemon=True, name="bootstrap").start()
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


def _filters(
    estado: str = "all",
    segment: str = "all",
    canal: str = "all",
    op: str = "all",
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    rating: str = "all",
    search: str = "",
    motivo: str = "all",
    inactivos: str = "ocultar",
    ambiente: str = Query("todos", pattern="^(todos|jugador|agente|sin_clasificar)$"),
) -> dict:
    """Filtros del dashboard (matchBase del front) como dependencia común. `from`/`to`
    llegan como alias porque `from` es palabra reservada en Python.

    JERARQUÍA (definida por el negocio el 2026-08-07): el filtro MAYOR es OPERADORES
    (`inactivos`), y adentro de eso el AMBIENTE. `segment` queda como filtro fino dentro
    del ambiente; los dos componen.

    `inactivos`: 'ocultar' (default) esconde a los operadores apagados de TODO lo que sale
    de conversation_scores; 'incluir' los trae de vuelta. La baja es lógica, así que la
    salida tiene que existir.

    `ambiente`: 'todos' (default: no esconder nada sin que alguien lo pida) | 'jugador' |
    'agente' | 'sin_clasificar'. Se valida con `pattern` para que un typo devuelva 422 en
    vez de degradarse al tablero completo: un número de la audiencia equivocada es peor
    que un error, porque nadie lo nota."""
    return {"estado": estado, "segment": segment, "canal": canal, "op": op,
            "date_from": date_from, "date_to": date_to, "rating": rating,
            "search": search, "motivo": motivo, "inactivos": inactivos,
            "ambiente": ambiente}


@app.get("/api/scores")
def scores(account: str = Query(..., description="datos | sistemas"),
           filters: dict = Depends(_filters)) -> list[dict]:
    """Conversaciones scoreadas de una cuenta (sin transcript).

    Era el único endpoint de lectura que ignoraba TODO filtro y devolvía la cuenta
    entera; con el switch de ambiente eso mezclaba las audiencias sin manera de recortar.
    Definido DESPUÉS de `_filters` a propósito: `Depends` se evalúa al definir la función."""
    with _conn() as c, c.cursor() as cur:
        return queries.scored_rows(cur, account, **filters)


@app.get("/api/options")
def options(account: str = Query(..., description="datos | sistemas"),
            ambiente: str = Query("todos",
                                  pattern="^(todos|jugador|agente|sin_clasificar)$")) -> dict:
    """Valores de los desplegables de filtros (segmento/canal/operador/motivo).

    Recortados por AMBIENTE: en `agente` el motivo es NULL (se califica con agilidad), así
    que ofrecer los 7 motivos era prometer un filtro que devuelve cero filas sin decir por
    qué. Estable por cuenta+ambiente -> el front lo pide una vez por combinación."""
    with _conn() as c, c.cursor() as cur:
        return queries.filter_options(cur, account, ambiente=ambiente)


@app.get("/api/summary")
def summary(account: str = Query(..., description="datos | sistemas"),
            filters: dict = Depends(_filters)) -> dict:
    """Agregados de las tarjetas (KPIs, distribución, operadores, depósito por canal)
    calculados en la BD para el filtro dado. Reemplaza el cómputo en memoria del front
    sobre las ~113k filas de /api/scores."""
    with _conn() as c, c.cursor() as cur:
        return queries.summary(cur, account, **filters)


@app.get("/api/tickets")
def tickets(account: str = Query(..., description="datos | sistemas"),
            page: int = Query(1, ge=1),
            sort: str = Query("new", description="new | old | best | worst"),
            filters: dict = Depends(_filters)) -> dict:
    """Una página de la lista de tickets (persona + conversaciones), agrupada,
    ordenada y paginada en la BD."""
    with _conn() as c, c.cursor() as cur:
        return queries.tickets_page(cur, account, page=page, sort=sort, **filters)


@app.get("/api/conversion")
def conversion(account: str = Query(..., description="datos | sistemas"),
               filters: dict = Depends(_filters)) -> dict:
    """Conversión jugador potencial->jugador (agrega player_conversions): ranking por
    operador + serie mensual. Filtrable por canal/segmento/operador/fecha de entrada.

    SOLO APLICA a jugadores. `player_conversions` se precomputa con las colas del segmento
    jugador y guarda `segment='jugador'` fijo, asi que antes del 2026-08-07 este endpoint
    devolvia los MISMOS datos para cualquier ambiente: apretabas "Agentes" y las tarjetas
    seguian mostrando jugadores, sin aviso (verificado: hash md5 identico en los tres).
    Ahora se DECLARA con `aplica` y el front pone el cartel en vez de datos de otra
    audiencia. Es una metrica que solo existe para una audiencia, no un filtro que falte."""
    ambiente = filters.get("ambiente", "todos")
    if not queries.conversion_aplica(ambiente):
        return {"aplica": False, "ambiente": ambiente,
                "by_operator": None, "by_month": None, "evolution": None}
    with _conn() as c, c.cursor() as cur:
        return {
            "aplica": True, "ambiente": ambiente,
            "by_operator": queries.conversion_by_operator(cur, account, **filters),
            "by_month": queries.conversion_by_month(cur, account, **filters),
            "evolution": queries.conversion_passivity_evolution(cur, account, **filters),
        }


@app.get("/api/conversion/cohort")
def conversion_cohort(account: str = Query(..., description="datos | sistemas"),
                      filters: dict = Depends(_filters)) -> list[dict]:
    """Drill-down: personas (jugadores nuevos) de la cohorte filtrada (p. ej. un
    operador) con la llave para abrir su conversación de entrada."""
    with _conn() as c, c.cursor() as cur:
        return queries.conversion_cohort(cur, account, **filters)


@app.get("/api/charts")
def charts(account: str = Query(..., description="datos | sistemas"),
           inactivos: str = "ocultar",
           ambiente: str = Query("jugador",
                                 pattern="^(todos|jugador|agente|sin_clasificar)$")) -> dict:
    """Agregados FULL-SCALE para los cuadros del análisis (deterministas; no dependen del
    scoring LLM): carga por operador, % depósito en WhatsApp por operador y contactos
    nuevos vs % depósito por mes.

    Estos cuadros ignoran los filtros del dashboard a propósito (ventana fija), con DOS
    excepciones: `inactivos` y `ambiente`.
    - `inactivos`: si no la respetaran, un operador apagado seguiría apareciendo acá y la
      baja lógica tendría un agujero justo a la vista.
    - `ambiente`: hasta el 2026-08-07 los tres cuadros estaban CLAVADOS a las colas del
      segmento jugador, que son el 24,2% de los comprobantes de depósito. El 71,7% lo
      carrean los agentes y no aparecía en ningún cuadro. El default sigue siendo
      'jugador' para no cambiarle la lectura a nadie sin avisar.

    Devuelve el `ambiente` aplicado junto a los datos: el origen viaja CON el número, así
    el front rotula lo que de verdad se contó en vez de asumirlo."""
    win = cfg.charts_window_months
    with _conn() as c, c.cursor() as cur:
        return {
            "load_by_operator": queries.load_by_operator(cur, account, window_months=win,
                                                         inactivos=inactivos, ambiente=ambiente),
            "deposit_pct_by_operator": queries.deposit_pct_by_operator(cur, account, window_months=win,
                                                                       inactivos=inactivos,
                                                                       ambiente=ambiente),
            "new_vs_deposit_by_month": queries.new_vs_deposit_by_month(cur, account, window_months=win,
                                                                       ambiente=ambiente),
            "window_months": win,
            "ambiente": ambiente,
        }


@app.get("/api/ambientes")
def ambientes(account: str = Query(..., description="datos | sistemas")) -> dict:
    """Qué hay ADENTRO de cada ambiente en esta cuenta: colas, segmentos y sesiones.

    Es la respuesta a "no se sabe de qué son qué": el front puede decir literalmente qué
    compone el número que está mostrando, en vez de que el usuario lo deduzca. Estable por
    cuenta -> se pide una vez, como /api/options (el agregado recorre las sesiones y no
    tiene por qué correr en cada cambio de filtro)."""
    with _conn() as c, c.cursor() as cur:
        return queries.ambiente_composition(cur, account)


@app.get("/api/conversation/{cid}")
def conversation(cid: str) -> dict:
    """Detalle completo de una conversacion + transcript (on-demand)."""
    with _conn() as c, c.cursor() as cur:
        detail = queries.conversation_detail(cur, cid)
    if detail is None:
        raise HTTPException(status_code=404, detail="conversacion no encontrada")
    return detail


# =============================================================================
# Operadores: prender/apagar (baja lógica). LECTURA abierta, ESCRITURA con token.
#
# La lectura no lleva token porque expone lo mismo que los cuadros ya muestran (nombres y
# volumen): una barrera ahí no protegería nada. La escritura sí, porque apagar operadores
# cambia lo que todo el mundo ve.
# =============================================================================
def require_admin(token: str | None = Header(None, alias="X-Admin-Token")) -> None:
    """Token compartido (DASHBOARD_ADMIN_TOKEN). FALLA CERRADA: si no está configurado,
    escribir es imposible. Al revés —abierto por default— un despliegue al que se le olvidó
    la variable dejaría a cualquiera apagando operadores sin que nadie se entere.

    `compare_digest` y no `==`: comparar secretos con == filtra su largo y su prefijo por
    el tiempo de respuesta."""
    if not cfg.admin_token:
        raise HTTPException(
            status_code=503,
            detail="escritura deshabilitada: falta configurar DASHBOARD_ADMIN_TOKEN",
        )
    if not token or not secrets.compare_digest(token, cfg.admin_token):
        raise HTTPException(status_code=401, detail="token invalido o ausente")


class OperadorFlag(BaseModel):
    operador: str
    activo: bool


class OperadoresUpdate(BaseModel):
    account: str
    operadores: list[OperadorFlag]

    @field_validator("account")
    @classmethod
    def _cuenta_no_vacia(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("account no puede estar vacio")
        return v.strip()

    @field_validator("operadores")
    @classmethod
    def _nombres_no_vacios(cls, v: list[OperadorFlag]) -> list[OperadorFlag]:
        # Un nombre vacío crearía una fila fantasma que nunca matchea a nadie y que
        # tampoco se puede borrar desde la UI.
        for op in v:
            if not op.operador.strip():
                raise ValueError("hay un operador con nombre vacio")
        return v


@app.get("/api/operators")
def operators(account: str = Query(..., description="datos | sistemas")) -> dict:
    """Operadores de la cuenta con su actividad y si están prendidos. Alimenta el modal."""
    with _conn() as c, c.cursor() as cur:
        operators_status.ensure_table(cur)
        filas = operators_status.admin_rows(cur, account)
    return {
        "account": account,
        "operadores": filas,
        # El umbral con el que el modal pre-marca al apretar "sugerir por actividad". 100 en
        # 30 dias reproduce exacto la lista de activos que dio el negocio (validado sobre
        # los datos reales de las dos cuentas).
        "umbral_sugerido": 100,
        "dias_sugeridos": 30,
        "escritura_habilitada": bool(cfg.admin_token),
    }


@app.put("/api/operators", dependencies=[Depends(require_admin)])
def operators_update(body: OperadoresUpdate) -> dict:
    """Prende/apaga operadores de UNA cuenta, en tanda. Baja LOGICA: no borra nada, solo
    los saca de los cuadros."""
    pares = [(op.operador.strip(), op.activo) for op in body.operadores]
    with _conn() as c:
        with c.cursor() as cur:
            operators_status.ensure_table(cur)
            n = operators_status.set_many(cur, body.account, pares)
        c.commit()
    return {"account": body.account, "actualizados": n}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    """El dashboard es una herramienta interna (datos de clientes/operadores): que
    los buscadores NO lo indexen. Además saca el 404 de ruido en los logs."""
    return "User-agent: *\nDisallow: /\n"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
