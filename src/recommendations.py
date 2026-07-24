"""Capa 1 de recomendaciones deterministas (sin LLM) para el coaching del agente.

La recomendacion de hoy sale VERBATIM del LLM (`recomendacion` en scorer.py), pero
el modelo casi nunca produce ciertos consejos de alto valor porque no los "ve"
como oportunidad (p. ej. avisar que hay que cambiar la contrasena cuando la
cuenta se creo desde el operador). Esta capa AUGMENTA la recomendacion del LLM
anteponiendo fragmentos deterministas de alta prioridad que se calculan a partir
de senales puras sobre los mensajes (ver src/signals.py).

Es coaching ASPIRACIONAL para el agente: NUNCA afecta la nota, la etiqueta ni
ningun otro campo del ScoreResult. Solo cambia el texto de `recomendacion`.
"""
from __future__ import annotations

from src.signals import agent_sent_credentials, agent_sent_register_link, app_mentioned

_FRAG_PASSWORD = (
    "Como la cuenta se creó desde el operador, indícale al cliente que cambie "
    "la contraseña en su primer ingreso por seguridad."
)
_FRAG_APP = (
    "No hay app disponible por ahora; guía al cliente a usar la web (la app "
    "estará disponible próximamente)."
)
_FRAG_REGISTER_LINK = (
    "Envía tu enlace de registro de Sorti con tu código de afiliado; explicar "
    "cómo entrar sin el enlace deja el alta a medias."
)


def refine_recomendacion(recomendacion: str, *, motivo: str, target_messages: list[dict]) -> str:
    """Antepone fragmentos deterministas de alto valor a la recomendacion del LLM.

    Calcula senales deterministas sobre `target_messages` (credenciales entregadas
    por el agente, enlace de registro enviado, mencion de la app) y arma una lista
    de fragmentos de coaching PRIORITARIOS que el LLM suele omitir. Si hay
    fragmentos, LIDERAN el texto final y la `recomendacion` del LLM (si no esta
    vacia) queda al final. Si no hay fragmentos, se devuelve `recomendacion` tal
    cual, sin tocarla.

    Es puramente aditivo/textual: no modifica la nota, la etiqueta ni ningun otro
    hecho del scoring.
    """
    cred = agent_sent_credentials(target_messages)
    reg_link = agent_sent_register_link(target_messages)
    app = app_mentioned(target_messages)

    fragmentos: list[str] = []
    if cred:
        fragmentos.append(_FRAG_PASSWORD)
    if app:
        fragmentos.append(_FRAG_APP)
    if motivo == "registro" and not reg_link and not cred:
        fragmentos.append(_FRAG_REGISTER_LINK)

    if not fragmentos:
        return recomendacion

    combinado = " ".join(fragmentos)
    if recomendacion:
        combinado = f"{combinado} {recomendacion}"
    return combinado
