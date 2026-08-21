"""El MOTIVO de una sesion del segmento `agente`. Clasifica; NO califica ni rutea.

EL HUECO QUE VIENE A CERRAR. El segmento `agente` es el mas grande del sistema -- 68.094
sesiones, 61.949 evaluadas -- y **todas tienen `motivo = NULL`**: las califica
`determinista/agilidad-v1`, que mide unicamente el reloj. El manual, en cambio, le dedica un
capitulo entero y una seccion literal, "Procesos que si gestionamos para agentes":

    procesamiento de recargas          -> `deposito`
    procesamiento de pagos             -> `retiro`
    recepcion y tramitacion de reclamos-> `problema` (ver mas abajo por que no entra)
    solicitudes de diseño              -\\
    solicitudes especiales de servicios -+-> `info`, por decision del negocio (2026-08-21)
    revision de info o inconsistencias  |
    apoyo en solicitudes operativas    -/

LA GENERALIZACION A `info` ES UNA DECISION, con su criterio: todo eso **es gente preguntando
por algo**, y es preferible a inventar seis motivos de poco volumen cada uno. Medido por
patron sobre las 68.094 sesiones (cota SUPERIOR, no verdad): comision/meta/arrastre 1.383
(2,0%) -- el mas grande, y el manual le dedica cuatro secciones --, diseño 745 (1,1%),
interesado en ser agente 437 (0,6%), clave o datos del Back Office 89, cierre o reingreso de
agencia 21, inconsistencias 8.

EL RUTEO VIVE EN src/worker.py Y SE MIDIO ANTES DE ACTIVARLO. `agilidad` existe porque correr
el pase con LLM en este segmento aplicaba la vara COMERCIAL del jugador (uplift,
empujo/pasivo) y **topaba el 94% de las sesiones de agente en 3 estrellas por diseño**. El
ruteo NO toca eso: manda a rubricas DETERMINISTAS, nunca al LLM, y `agilidad` se queda con lo
que no tiene motivo probable.

`problema` NO ENTRA, y no por olvido: **no hay una sola señal determinista de reclamo en el
repo**. Es el unico motivo sin rubrica determinista, el que siempre cae al LLM. Clasificarlo
aca exigiria una señal nueva o el modelo, y las dos son otro cambio. Un reclamo por la
comision cae hoy en `info`, que es impreciso pero no es una acusacion: `info` juzga si la
respuesta fue correcta y a tiempo, no si el reclamo se tramito.

EL COACHING DE `info` YA TIENE VARIANTE DE AGENTE (C36-C39). Los textos del jugador hablaban
de alguien "decidiendo si se queda" y "comparando", y un agente es un socio con contrato. Y no
era solo redaccion: el manual le da al agente otra regla de cierre, asi que el consejo del 4
le pide /FIN y los 5 minutos en vez de la pregunta. La clave del catalogo es
(rubrica, situacion, segmento).
"""
from __future__ import annotations

import re
import unicodedata

from src.deposito import es_transaccion as es_transaccion_deposito
from src.retiro import es_transaccion as es_transaccion_retiro

# Los temas propios del agente que el negocio decidio mandar a `info`. SON los que deciden:
# se exige el TEMA y no una pregunta generica, porque en este segmento el pedido tambien viene
# con signo de interrogacion ("me cargas 30 a la agencia?") y eso no es una consulta. Y no se
# depende de la puntuacion en el otro sentido: "cuanto me quedo de comision" no lleva signo, y
# en la data real el agente escribe corrido.
_TEMAS_DE_AGENTE = re.compile(
    r"comisi[oó]n|arrastre|mi meta|porcentaje base"
    r"|dise[nñ]o|flyer|banner|arte|logo|video personalizado|auspicio"
    r"|ser agente|abrir (una )?agencia|interesad\w+ en ser"
    r"|back ?office|sorti ?center|clave de (mi )?agencia"
    r"|cerrar mi agencia|reingres\w+|reactivar (mi )?agencia"
    r"|inconsistencia|no me cuadra",
    re.IGNORECASE,
)


def _norm(s: str | None) -> str:
    """Sin acentos y en minusculas: `re.IGNORECASE` no dobla los acentos, y esa asimetria ya
    costo un bug en src/redireccion.py."""
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _pregunto_un_tema_de_agente(messages: list[dict]) -> bool:
    for m in messages:
        if m.get("from_me") or m.get("is_note"):
            continue
        if _TEMAS_DE_AGENTE.search(_norm(m.get("body"))):
            return True
    return False


def motivo_de_agente(messages: list[dict]) -> str | None:
    """El motivo de la sesion, o None si no se puede probar ninguno.

    None significa "se lo queda `agilidad`", y es una respuesta legitima: el 12% de las
    sesiones de agente no tiene ninguna señal, y no se les inventa un motivo para que la fila
    se vea completa.

    LA TRANSACCION LE GANA A LA PREGUNTA. Si hubo plata movida, el motivo es la operacion:
    una pregunta suelta en el mismo hilo no la tapa. Y entre las dos transacciones manda
    `deposito`, igual que en el camino del jugador (el guard de src/scorer.py): 14 de 400
    sesiones de agente medidas tienen las dos, y la recarga es la que define la sesion en una
    caja.
    """
    if not messages:
        return None
    if es_transaccion_deposito(messages):
        return "deposito"
    if es_transaccion_retiro(messages):
        return "retiro"
    # `info` EXIGE UN TEMA PROPIO DEL AGENTE, no cualquier pregunta. La primera version
    # aceptaba `client_asked_question` a secas y eso rompia el caso mas comun del segmento:
    # "me cargas 30 a la agencia?" es un PEDIDO que termina en signo de interrogacion, no una
    # consulta. Mandarlo a `info` le pregunta "respondio la consulta de forma correcta y
    # completa" cuando lo que habia que evaluar era si CUMPLIO el pedido -- y eso es
    # exactamente lo que mide `agilidad`.
    # Un pedido de recarga sin comprobante no es transaccion (no hay que acreditar nada
    # todavia) y tampoco es consulta: se lo queda `agilidad`, que es su lugar.
    if _pregunto_un_tema_de_agente(messages):
        return "info"
    return None
