"""Configuracion desde variables de entorno (ver .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    ollama_url: str
    ollama_model: str
    api_host: str
    api_port: int
    log_level: str


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
    )
