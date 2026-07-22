"""Senales deterministas de RESOLUCION del agente (capa sin LLM).

Corrige la dureza sistematica del modelo detectada en la auditoria: el LLM hunde
por debajo del piso interacciones donde el agente SI atendio el motivo, porque
(a) confirmo la transaccion con una plantilla ("ing"/"listo"/"saldo disponible"),
(b) mando el comprobante/tutorial como media que el modelo no puede leer, o
(c) el cliente abandono despues de una respuesta accionable.

Estas funciones puras dan la evidencia determinista para que el scorer aplique un
PISO (nunca sube a buena/excelente; solo evita el deficiente/mala injusto) y para
que el router no saltee un deposito estandar como 'customer_media_only'.

Mensajes = dicts con: from_me, is_note, body, media_type, sent_from.
Se evalua SOLO al agente HUMANO (from_me, no nota, sent_from != CHATBOT).
"""
from __future__ import annotations

import re

from src.metrics import _is_bot

# Confirmacion transaccional del agente. Tokens reales del dataset (plantillas y
# taquigrafia de operador): "ing"/"ingreso"/"ingresado", "acreditado", "cargado",
# "realizado/procesado/reflejado/abonado", "listo", "en breve", "disponible"
# (saldo disponible). Deliberadamente SIN tokens genericos ("hecho") para no
# floorear conversaciones que no son una confirmacion. Se aplica solo a motivos
# transaccionales, asi que dentro de ese contexto estos tokens son confirmaciones.
CONFIRMATION_PATTERN = (
    r"\b(ing|ingr|ingres[oó]?|ingresad[oa]s?|acredit\w*|cargad[oa]s?|carg[oó]|"
    r"realizad[oa]s?|procesad[oa]s?|reflejad[oa]s?|abonad[oa]s?|listo|en breve|disponible)\b"
)
_CONFIRMATION_RE = re.compile(CONFIRMATION_PATTERN, re.IGNORECASE)


def _is_agent(m: dict) -> bool:
    """Agente humano: enviado por el negocio (from_me), no nota, no bot."""
    return bool(m.get("from_me")) and not m.get("is_note") and not _is_bot(m)


def agent_confirmation(messages: list[dict]) -> bool:
    """True si algun mensaje del AGENTE confirma la transaccion (token de plantilla)."""
    return any(
        _CONFIRMATION_RE.search(m.get("body") or "")
        for m in messages
        if _is_agent(m)
    )


def agent_sent_media(messages: list[dict]) -> bool:
    """True si el AGENTE mando media (comprobante de retiro, tutorial, etc.).

    El modelo no puede leer la media; asumir fracaso por eso es el error #3 de la
    auditoria. Si el agente la mando, es evidencia de que atendio.
    """
    return any(_is_agent(m) and (m.get("media_type") or "").strip() for m in messages)


def client_abandoned(messages: list[dict]) -> bool:
    """True si el ULTIMO mensaje real (sin notas) lo mando el agente.

    Es decir, el cliente no volvio a responder tras la ultima intervencion del
    agente: la falta de cierre es del cliente, no del agente (trampa #2).
    """
    real = [m for m in messages if not m.get("is_note")]
    if not real:
        return False
    return bool(real[-1].get("from_me"))


def agent_resolved(messages: list[dict]) -> bool:
    """El agente atendio el motivo de forma determinista: confirmo o mando media.

    Senal combinada que usan el scorer (piso) y el router (no skipear un deposito
    estandar donde el cliente solo mando el comprobante).
    """
    return agent_confirmation(messages) or agent_sent_media(messages)


# Empuje comercial del agente (eje 'atencion'=empujo): manda un LINK (registro/
# recarga), invita explicitamente, o presenta un bono ATADO a una recarga. La
# auditoria mostro que el modelo marca 'pasivo' aunque el agente claramente empuja.
PUSH_PATTERN = (
    r"https?://|t[ei] invit|aprovech|no te pierdas|reg[íi]strate|"
    r"obten[eé]s un bono|obtienes un bono|por tu (primera|segunda|pr[oó]xima) recarga"
)
_PUSH_RE = re.compile(PUSH_PATTERN, re.IGNORECASE)

# Maltrato GRAVE del agente (unico gatillo legitimo de 'mala'=1 estrella). Patron
# DELIBERADAMENTE conservador y de alta precision: insultos/agresion explicitos.
# Casi nunca dispara (el maltrato del agente es rarisimo), asi que 'mala' queda
# reservado a evidencia real y todo lo demas cae a 'deficiente' (ver scorer).
MALTRATO_PATTERN = (
    r"\b(idiota|est[uú]pid\w*|imb[eé]cil|c[aá]llate|no me molest\w*|no jodas|"
    r"grosero|malcriado|no seas \w+|dej[aá] de fregar|l[aá]rgate|no me interesa tu)\b"
)
_MALTRATO_RE = re.compile(MALTRATO_PATTERN, re.IGNORECASE)


def agent_pushed(messages: list[dict]) -> bool:
    """True si el AGENTE empujo conversion/retencion (link, invitacion, bono por recarga)."""
    return any(_PUSH_RE.search(m.get("body") or "") for m in messages if _is_agent(m))


def agent_maltrato(messages: list[dict]) -> bool:
    """True si hay maltrato GRAVE del agente (insulto/agresion explicita)."""
    return any(_MALTRATO_RE.search(m.get("body") or "") for m in messages if _is_agent(m))
