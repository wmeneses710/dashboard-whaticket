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
    """AGREGA fragmentos deterministas de alto valor al final de la recomendacion del LLM.

    Calcula senales deterministas sobre `target_messages` (credenciales entregadas
    por el operador, mencion de la app) y arma una lista de fragmentos de coaching que
    el LLM suele omitir. Si no hay fragmentos, se devuelve `recomendacion` tal cual.

    EL ORDEN SE INVIRTIO el 2026-08-07. Antes los fragmentos LIDERABAN y el consejo del
    modelo quedaba atras. Medido con el modelo de prod sobre 45 sesiones: 3 (6,7%)
    arrancaban con "No hay app disponible por ahora..." en sesiones de `problema` y
    `registro`, donde la app no era el tema — en una de registro el consejo util ("guia al
    cliente paso a paso para crear la cuenta") quedaba DETRAS de un anuncio que nadie
    pidio. Es la misma familia del `_FRAG_REGISTER_LINK` retirado (ver arriba): un
    fragmento generico ganandole al juicio contextual del modelo. Ahora el modelo lidera y
    el fragmento es un apendice.

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

    apendice = " ".join(fragmentos)
    return f"{recomendacion} {apendice}" if recomendacion else apendice
