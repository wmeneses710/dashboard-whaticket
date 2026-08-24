"""Configuracion desde variables de entorno (ver .env.example).

Carga un archivo `.env` si existe (dotenv) y despues lee de os.environ. Las
variables ya presentes en el entorno (p. ej. las que inyecta EasyPanel en el
panel de despliegue) TIENEN PRECEDENCIA: load_dotenv no las pisa. Asi, `.env`
sirve para local y el panel manda en prod.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # no-op si no hay .env; no sobreescribe variables ya definidas


@dataclass(frozen=True)
class Config:
    database_url: str
    ollama_url: str
    ollama_model: str
    ollama_token: str  # auth para un Ollama detras de proxy (Cloudflare); "" = sin auth
    # Tuning de inferencia (para caber bajo el timeout del proxy, p. ej. 100s de Cloudflare):
    ollama_num_ctx: int         # ventana de contexto del modelo
    ollama_num_predict: int     # tope de tokens de SALIDA (el decode secuencial manda el tiempo)
    llm_fast_attempts: int      # intentos del camino rapido antes del fallback lento
    api_host: str
    api_port: int
    log_level: str
    # LA API AUTODOCUMENTADA, APAGADA POR DEFECTO. El tablero vive en un dominio publico y
    # las lecturas son anonimas, asi que /openapi.json le entrega a cualquiera el mapa de que
    # pedir para leer nombres, telefonos y transcripts. En local se prende con API_DOCS=true;
    # el default tiene que ser el seguro, no el comodo.
    api_docs: bool
    # --- Worker de scoring (mismo contenedor, configurable en EasyPanel) ---
    scoring_enabled: bool
    scoring_accounts: tuple[str, ...]
    scoring_batch_size: int
    scoring_poll_seconds: int
    # Sub-evaluadores angostos (2da pasada del LLM), opt-in (cuestan llamadas extra):
    recom_subagent_enabled: bool  # genera la recomendación con un pase dedicado de coaching
    # Ventana móvil de los cuadros: cuántos meses (los más recientes) se muestran.
    charts_window_months: int
    # Token compartido para los endpoints de ESCRITURA (hoy: prender/apagar operadores).
    # VACIO = escritura DESHABILITADA (falla cerrada). Generalo con
    # `python scripts/gen_admin_token.py` y ponelo en el .env / en el panel de EasyPanel.
    admin_token: str


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = tuple(p.strip() for p in value.split(",") if p.strip())
    return items or default


def load_config() -> Config:
    return Config(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://whaticket:whaticket@localhost:5432/whaticket"
        ),
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"),
        ollama_token=os.environ.get("OLLAMA_TOKEN", ""),
        ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "16384")),
        ollama_num_predict=int(os.environ.get("OLLAMA_NUM_PREDICT", "768")),
        llm_fast_attempts=int(os.environ.get("LLM_FAST_ATTEMPTS", "2")),
        api_host=os.environ.get("API_HOST", "0.0.0.0"),
        api_port=int(os.environ.get("API_PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        api_docs=_bool(os.environ.get("API_DOCS")),
        scoring_enabled=_bool(os.environ.get("SCORING_ENABLED")),
        scoring_accounts=_csv(os.environ.get("SCORING_ACCOUNTS"), ("sistemas", "datos")),
        scoring_batch_size=int(os.environ.get("SCORING_BATCH_SIZE", "20")),
        scoring_poll_seconds=int(os.environ.get("SCORING_POLL_SECONDS", "60")),
        recom_subagent_enabled=_bool(os.environ.get("SCORING_RECOM_SUBAGENT")),
        charts_window_months=int(os.environ.get("CHARTS_WINDOW_MONTHS", "12")),
        admin_token=os.environ.get("DASHBOARD_ADMIN_TOKEN", "").strip(),
    )
