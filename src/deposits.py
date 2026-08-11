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
#
# VOCABULARIO REAL DEL CLIENTE (auditoria del 2026-08-11): el cliente de este negocio no
# dice "recargar". Dice "cargar" ("Cargar como agente"), "recargueme" (con acento, que
# `recarg` no matcheaba) o "acreditando" ("ayudeme acreditando"). Medido sobre la copia
# de prod: 2.664 sesiones CON comprobante del cliente quedaban fuera del gate solo por
# esas tres formas.
#   - `c[aá]rg[au]` con FRONTERA IZQUIERDA y terminacion acotada: `carg` suelto matcheaba
#     "descargar"/"encargado"/"a cargo" en 1.095 mensajes del cliente (bajar la app no es
#     una recarga). Con la frontera y el [au] quedan 26.
#   - `saldo` queda DELIBERADAMENTE afuera: "acreditame el saldo" es una recarga pero
#     "cuanto tengo de saldo" es una consulta de `info`. Sumaba solo 571 sesiones y no
#     justifica el falso positivo.
RECHARGE_PATTERN = (
    r"rec[aá]rg|comprobante|dep[oó]sit|transferenc|abono|acredit|"
    r"(^|[^a-záéíóúüñ])c[aá]rg[au]"
)
_RECHARGE_RE = re.compile(RECHARGE_PATTERN, re.IGNORECASE)


def _is_customer(m: dict) -> bool:
    return not m.get("from_me") and not m.get("is_note")


def _is_image(m: dict) -> bool:
    return "image" in (m.get("media_type") or "").lower()


def has_recharge_context(messages: list[dict]) -> bool:
    """True si el CLIENTE menciona una razon de recarga.

    Solo el cliente: la plantilla de venta del operador habla de recargar en casi
    toda conversacion de prospeccion ("con tu primera carga comienza a disfrutar",
    "depositas 5 amiga y recibes 5 mas", "las cargas y los retiros se hacen por
    transferencia"). Leyendo tambien al operador, el gate pasaba de 519 a 885
    sesiones sobre la misma muestra (41,4% inflado, medido el 2026-08-06) y metia
    como "deposito" conversaciones de `promo` y `registro` donde el cliente solo
    habia mandado una imagen cualquiera. El contexto lo tiene que poner quien viene
    a recargar, no quien se lo ofrece.
    """
    return any(
        _RECHARGE_RE.search(m.get("body") or "")
        for m in messages
        if not m.get("is_note") and not m.get("from_me")
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
