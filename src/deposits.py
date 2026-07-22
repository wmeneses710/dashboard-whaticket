"""Deteccion determinista de comprobantes de recarga (el "deposito" del analisis).

El deposito NO es monto: es cuantas VECES el cliente manda un COMPROBANTE (imagen)
junto con la RAZON (recarga). Esta capa es el GATE determinista:

  - Cuenta las imagenes del CLIENTE (from_me=False, no nota) -> los comprobantes.
  - Solo si la conversacion tiene contexto de recarga (keyword) -> evita falsos
    positivos por imagenes sueltas (una foto cualquiera no es un comprobante).

Es el techo de candidatos; la confirmacion final (comprobante efectivamente
acreditado, no un "quiero recargar") la hace el LLM SOLO dentro de lo elegible.

Mensajes = dicts con: from_me, is_note, body, media_type.
"""
from __future__ import annotations

import re

# Razon de recarga en el texto (tolera acentos y mayusculas). Fuente unica del
# patron: lo reusa la deteccion en Python (aca) y la agregacion full-scale en SQL
# (src.queries, via regexp `~*`). No duplicar.
# 'abono' agregado: el flujo "Abono N a deuda" (cliente manda comprobante para que
# le acrediten saldo) es una recarga de altisimo volumen que el patron viejo no veia
# -> el gate no disparaba y esas sesiones caian mal clasificadas como 'problema'
# (auditoria). Cubre tambien el subconteo de deposit_count. Se reusa en SQL (src.queries).
RECHARGE_PATTERN = r"recarg|comprobante|dep[oó]sit|transferenc|abono"
_RECHARGE_RE = re.compile(RECHARGE_PATTERN, re.IGNORECASE)


def _is_customer(m: dict) -> bool:
    return not m.get("from_me") and not m.get("is_note")


def _is_image(m: dict) -> bool:
    return "image" in (m.get("media_type") or "").lower()


def has_recharge_context(messages: list[dict]) -> bool:
    """True si algun mensaje (no nota) menciona una razon de recarga."""
    return any(
        _RECHARGE_RE.search(m.get("body") or "")
        for m in messages
        if not m.get("is_note")
    )


def receipt_image_count(messages: list[dict]) -> int:
    """Cantidad de imagenes enviadas por el CLIENTE (comprobantes candidatos)."""
    return sum(1 for m in messages if _is_customer(m) and _is_image(m))


def deposit_candidate_count(messages: list[dict]) -> int:
    """Depositos candidatos (VECES): imagenes del cliente, gateadas por contexto
    de recarga. Sin comprobante del cliente o sin razon de recarga -> 0."""
    if not has_recharge_context(messages):
        return 0
    return receipt_image_count(messages)


def is_deposit_candidate(messages: list[dict]) -> bool:
    return deposit_candidate_count(messages) > 0
