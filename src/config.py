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
    api_host: str
    api_port: int
    log_level: str
    # --- Worker de scoring (mismo contenedor, configurable en EasyPanel) ---
    scoring_enabled: bool
    scoring_accounts: tuple[str, ...]
    scoring_batch_size: int
    scoring_poll_seconds: int
    # Ventana móvil de los cuadros: cuántos meses (los más recientes) se muestran.
    charts_window_months: int


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
        api_host=os.environ.get("API_HOST", "0.0.0.0"),
        api_port=int(os.environ.get("API_PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        scoring_enabled=_bool(os.environ.get("SCORING_ENABLED")),
        scoring_accounts=_csv(os.environ.get("SCORING_ACCOUNTS"), ("sistemas", "datos")),
        scoring_batch_size=int(os.environ.get("SCORING_BATCH_SIZE", "20")),
        scoring_poll_seconds=int(os.environ.get("SCORING_POLL_SECONDS", "60")),
        charts_window_months=int(os.environ.get("CHARTS_WINDOW_MONTHS", "12")),
    )
