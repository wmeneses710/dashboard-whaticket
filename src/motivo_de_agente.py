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

ESTE MODULO NO RUTEA, Y ESO ES DELIBERADO. `agilidad` existe porque correr el pase con LLM en
este segmento aplicaba la vara COMERCIAL del jugador (uplift, empujo/pasivo) y **topaba el 94%
de las sesiones de agente en 3 estrellas por diseño**. Re-rutear sin medir repite ese error.
Primero se clasifica, se mide donde caerian las notas, y despues se decide.

`problema` NO ENTRA, y no por olvido: **no hay una sola señal determinista de reclamo en el
repo**. Es el unico motivo sin rubrica determinista, el que siempre cae al LLM. Clasificarlo
aca exigiria una señal nueva o el modelo, y las dos son otro cambio. Un reclamo por la
comision cae hoy en `info`, que es impreciso pero no es una acusacion: `info` juzga si la
respuesta fue correcta y a tiempo, no si el reclamo se tramito.

ANTES DE ACTIVAR EL RUTEO, EL COACHING DE `info` NECESITA VARIANTE DE AGENTE. Sus textos
estan escritos para el jugador: "quien pregunta todavia esta decidiendo si se queda" (C06),
"quien consulta esta comparando" (C07). Un agente que pregunta por su comision es un socio con
contrato, no un prospecto. La clave del consejo en src/catalogo_coaching.py es
(rubrica, situacion), asi que hay lugar para la variante sin tocar la estructura.
"""
from __future__ import annotations

import re
import unicodedata

from src.deposito import es_transaccion as es_transaccion_deposito
from src.retiro import es_transaccion as es_transaccion_retiro
from src.signals import client_asked_question

# Los temas propios del agente que el negocio decidio mandar a `info`. NO deciden el motivo
# por si solos -- lo decide `client_asked_question` --, sirven para no depender de que la
# pregunta traiga un signo de interrogacion: "cuanto me quedo de comision" no lo tiene, y en
# la data real el agente escribe corrido y sin puntuacion.
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
    # `info` = el agente pregunto algo. Alcanza con la pregunta generica O con uno de los
    # temas propios del agente, porque en la data real escribe sin puntuacion.
    if client_asked_question(messages) or _pregunto_un_tema_de_agente(messages):
        return "info"
    return None
