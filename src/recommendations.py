"""Capa 1 de recomendaciones deterministas (sin LLM) para el coaching del operador.

La recomendacion de hoy sale VERBATIM del LLM (`recomendacion` en scorer.py), pero
el modelo casi nunca produce ciertos consejos de alto valor porque no los "ve"
como oportunidad (p. ej. avisar que hay que cambiar la contrasena cuando la
cuenta se creo desde el operador). Esta capa AUGMENTA la recomendacion del LLM
anteponiendo fragmentos deterministas de alta prioridad que se calculan a partir
de senales puras sobre los mensajes (ver src/signals.py).

Es coaching ASPIRACIONAL para el operador: NUNCA afecta la nota, la etiqueta ni
ningun otro campo del ScoreResult. Solo cambia el texto de `recomendacion`.
"""
from __future__ import annotations

from src.signals import operator_sent_credentials, app_mentioned

_FRAG_PASSWORD = (
    "Como la cuenta se creó desde el operador, indícale al cliente que cambie "
    "la contraseña en su primer ingreso por seguridad."
)
_FRAG_APP = (
    "No hay app disponible por ahora; guía al cliente a usar la web (la app "
    "estará disponible próximamente)."
)
# RETIRADO el 2026-08-06: habia un _FRAG_REGISTER_LINK que decia "Envia tu enlace de
# registro de Sorti con tu codigo de afiliado". El negocio lo marco como FALSO en las
# dos puntas: no existe codigo de afiliado, y el registro lo hace el OPERADOR, asi que
# mandar el link no viene al caso. Era el texto mas repetido de todo el coaching —
# **151 de 624 recomendaciones (24,2%)**, todas en `registro`— y no lo escribia el
# modelo: lo anteponia este modulo, por eso ningun ajuste de prompt lo sacaba.


def refine_recomendacion(recomendacion: str, *, motivo: str, target_messages: list[dict]) -> str:
    """Antepone fragmentos deterministas de alto valor a la recomendacion del LLM.

    Calcula senales deterministas sobre `target_messages` (credenciales entregadas
    por el operador, enlace de registro enviado, mencion de la app) y arma una lista
    de fragmentos de coaching PRIORITARIOS que el LLM suele omitir. Si hay
    fragmentos, LIDERAN el texto final y la `recomendacion` del LLM (si no esta
    vacia) queda al final. Si no hay fragmentos, se devuelve `recomendacion` tal
    cual, sin tocarla.

    Es puramente aditivo/textual: no modifica la nota, la etiqueta ni ningun otro
    hecho del scoring.
    """
    cred = operator_sent_credentials(target_messages)
    app = app_mentioned(target_messages)

    fragmentos: list[str] = []
    if cred:
        fragmentos.append(_FRAG_PASSWORD)
    if app:
        fragmentos.append(_FRAG_APP)

    if not fragmentos:
        return recomendacion

    combinado = " ".join(fragmentos)
    if recomendacion:
        combinado = f"{combinado} {recomendacion}"
    return combinado
