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
import unicodedata

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


# Tipos de media REAL (comprobante, tutorial en video, audio, doc). Se excluyen a
# proposito 'chat'/'missed'/'template'/'location', que NO son un adjunto del agente
# (un texto guardado como 'chat' no debe contar como "mando el comprobante/tutorial").
_MEDIA_TYPES = frozenset({"image", "video", "audio", "voice", "ptt", "document",
                          "application", "sticker", "viewonce"})


def agent_sent_media(messages: list[dict]) -> bool:
    """True si el AGENTE mando MEDIA real (comprobante de retiro, video-tutorial, etc.).

    El modelo no puede leer la media; asumir fracaso por eso es el error #3 de la
    auditoria. Si el agente la mando, es evidencia de que atendio.
    """
    return any(
        _is_agent(m) and (m.get("media_type") or "").strip().lower() in _MEDIA_TYPES
        for m in messages
    )


def client_abandoned(messages: list[dict]) -> bool:
    """True si el ULTIMO mensaje real (sin notas) lo mando el agente.

    Es decir, el cliente no volvio a responder tras la ultima intervencion del
    agente: la falta de cierre es del cliente, no del agente (trampa #2).
    """
    real = [m for m in messages if not m.get("is_note")]
    if not real:
        return False
    return bool(real[-1].get("from_me"))


# ¿El cliente planteó una CONSULTA contestable? (signo de pregunta o palabra interrogativa).
# Si NO, en 'info' no hay nada que "no responder": el piso se cumple respondiendo cordial
# (trampa de abandono/sin-necesidad). Evita el falso deficiente del saludo/gracias/abandono,
# SIN pisar el caso legítimo donde el cliente sí preguntó algo y el agente lo evadió.
# Se normalizan acentos (á->a) para no fallar por acentos compuestos/descompuestos o faltantes.
_Q_WORDS_RE = re.compile(
    r"\b(como|cuand|cuant|donde|que|cual|por que|se puede|puedo|"
    r"necesito saber|quiero saber|me explic|no se como)\b"
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def client_asked_question(messages: list[dict]) -> bool:
    """True si algún mensaje del CLIENTE contiene una consulta contestable."""
    for m in messages:
        if m.get("from_me") or m.get("is_note"):
            continue
        body = m.get("body") or ""
        if "?" in body or "¿" in body or _Q_WORDS_RE.search(_strip_accents(body)):
            return True
    return False


# Pings de DESESPERACION del cliente (re-pregunta/insistencia sin respuesta): solo
# signos de pregunta ("?"/"??"), o palabras de reclamo por silencio. Se normalizan
# acentos. Son la marca de que el cliente tuvo que insistir, no de una consulta normal.
_REASK_PING_RE = re.compile(
    r"\b(ayuda|auxilio|me responden?|respond[ae](me|n)?|alguien( ahi)?|"
    r"sigue[sn]? ahi|est[a]?[sn]? ahi|hol[ao]+\?)\b"
)


def _is_reask_ping(body: str) -> bool:
    """True si el mensaje del cliente es un ping de insistencia (solo '?' o reclamo de silencio)."""
    b = _strip_accents((body or "").strip().lower())
    if not b:
        return False
    if re.fullmatch(r"[?¿]+", b):
        return True
    return bool(_REASK_PING_RE.search(b))


def client_reasked(messages: list[dict], *, min_run: int = 4) -> bool:
    """True si hubo FRICCION: el cliente tuvo que reinsistir sin respuesta del negocio.

    Senal determinista de que el cliente quedo colgado (agnostica al motivo): una
    corrida de mensajes CONSECUTIVOS del cliente SIN respuesta del negocio (agente o
    bot). Dispara si la corrida llega a `min_run`, o si en una corrida de >=2 el
    cliente manda un ping de desesperacion ("?", "ayuda", "me responden"). El caso
    multi-transaccion (cliente manda mucho pero el agente contesta entre medio) NO
    dispara, porque cada respuesta del negocio corta la corrida.
    """
    run = 0
    run_has_ping = False
    for m in messages:
        if m.get("is_note"):
            continue
        if m.get("from_me"):  # el negocio (agente o bot) respondio -> corta la corrida
            run = 0
            run_has_ping = False
            continue
        run += 1  # mensaje del cliente
        if _is_reask_ping(m.get("body") or ""):
            run_has_ping = True
        if run >= min_run or (run >= 2 and run_has_ping):
            return True
    return False


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
    r"obten[eé]s un bono|obtienes un bono|por tu (primera|segunda|pr[oó]xima) recarga|"
    # ofrecer/guiar el alta y presentar promos cuenta como EMPUJO (aunque el motivo no sea
    # promo): 'te creo un usuario', 'te registro', menciones de bono/promo/freebet/giros.
    r"te creo (un |tu )?(usuario|cuenta)|creo tu (usuario|cuenta)|te (ayudo a )?registr|"
    r"te registro|\bbono\b|\bpromo|freebet|giros (gratis|de regalo)"
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
    """True si el AGENTE empujo conversion/retencion (link, invitacion, bono por recarga).

    Señal AMPLIA: sirve para el PISO del front-of-funnel (explicar la promo YA cuenta) y
    para el eje atencion. Para el UPLIFT (buena/excelente) es demasiado laxa -> usar
    agent_strong_uplift, que exige una accion concreta (no la mera explicacion de la promo).
    """
    return any(_PUSH_RE.search(m.get("body") or "") for m in messages if _is_agent(m))


# UPLIFT CONCRETO (para licenciar buena/excelente): un LINK, o una invitacion IMPERATIVA a
# convertir AHORA (depositar/recargar/registrarse/jugar). Deliberadamente NO incluye la mera
# mencion de "primer deposito"/"con tu primera carga" ni "aprovecha": eso está DENTRO de la
# plantilla de explicacion de la promo (= piso), no es un empuje concreto (dos conversaciones
# con la MISMA plantilla salian 3★ y 5★; el empuje real es el link o el imperativo).
STRONG_UPLIFT_PATTERN = (
    r"https?://|t[ei] invit[oa] a (deposit|recarg|jug|apost|registr)|"
    r"deposit[aá] (ya|ahora|hoy)|recarg[aá] (ya|ahora|hoy)|"
    r"reg[íi]strate (ya|ahora|aqu[ií]|en el|en este)|complet[aá] tu registro|"
    r"pas[aá]me (tu|los) (nombre|datos|c[eé]dula)|indic[aá]me (tu|el)"
)
_STRONG_UPLIFT_RE = re.compile(STRONG_UPLIFT_PATTERN, re.IGNORECASE)


def agent_strong_uplift(messages: list[dict]) -> bool:
    """True si el AGENTE hizo un empuje CONCRETO (link o invitacion explicita a convertir)."""
    return any(_STRONG_UPLIFT_RE.search(m.get("body") or "") for m in messages if _is_agent(m))


def agent_maltrato(messages: list[dict]) -> bool:
    """True si hay maltrato GRAVE del agente (insulto/agresion explicita)."""
    return any(_MALTRATO_RE.search(m.get("body") or "") for m in messages if _is_agent(m))
