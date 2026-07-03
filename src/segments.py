"""Mapeo cola (queueName) -> segmento de negocio.

La segmentacion NO es por cuenta sino por cola: una misma cuenta puede tener
jugadores y agentes. Cada segmento se evalua con una rubrica distinta
(jugador = conversion/atencion; agente = satisfaccion/resolucion).

Nombres de cola observados en la data real (jun-2026):
  sistemas: "Agente 👨👩", "Jugadores", "", "Departamento de Makerting", "Prueba"
  datos:    "OnlySorti", "sortiGO", "ModoSorti", ""

El matching es tolerante (minusculas, sin espacios) porque el nombre puede
variar entre exports ("OnlySorti" / "ONLY SORTI" / "onlysorti").
"""
from __future__ import annotations

Segment = str  # "jugador" | "agente" | "marketing" | "interno" | "descartar" | "otro"


def _normalize(name: str | None) -> str:
    """Minusculas y sin espacios, para comparar por substring de forma estable."""
    return "".join((name or "").split()).lower()


# Fragmentos normalizados que identifican cada segmento. Orden = prioridad.
_PLAYER_MARKERS = ("jugador", "onlysorti", "modosorti", "sortigo")
_AGENT_MARKERS = ("agente",)
_MARKETING_MARKERS = ("marketing", "makerting", "mercadeo")
_DISCARD_MARKERS = ("prueba", "test")


def segment_for_queue(queue_name: str | None) -> Segment:
    """Devuelve el segmento de negocio para un nombre de cola.

    Cola vacia/None = uso interno entre operadores. Cola de "Prueba" se
    descarta del analisis. Lo no reconocido cae en "otro" (no se pierde:
    se marca para revisar).
    """
    norm = _normalize(queue_name)
    if not norm:
        return "interno"
    if any(m in norm for m in _DISCARD_MARKERS):
        return "descartar"
    if any(m in norm for m in _PLAYER_MARKERS):
        return "jugador"
    if any(m in norm for m in _AGENT_MARKERS):
        return "agente"
    if any(m in norm for m in _MARKETING_MARKERS):
        return "marketing"
    return "otro"
