"""Mapeo cola (queueName) -> segmento de negocio.

El segmento dice QUIEN ESTA DEL OTRO LADO del chat (el cliente), NO quien atiende:
quien atiende es SIEMPRE un OPERADOR (personal de soporte, tabla `users`). Los dos
publicos principales:
  - jugador: el usuario final que apuesta.
  - agente:  el vendedor/afiliador que trae usuarios y opera una caja (carga y
             descarga saldo). Es un CLIENTE nuestro, no personal nuestro. En
             `sistemas` es la cola de mayor volumen ("Agente 👨👩").

La segmentacion NO es por cuenta sino por cola: una misma cuenta puede tener
jugadores y agentes.

OJO: hoy el segmento NO entra en el scoring. La rubrica se elige por MOTIVO
(src/rubrics.py) y `score_by_motivo` nunca recibe el segmento -> un agente se
califica con la misma vara comercial que un jugador (uplift = empujar registro/
deposito), que para un vendedor profesional no aplica. Es una deuda conocida.

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


# --- AMBIENTES: el agrupador GRUESO del dashboard --------------------------------
# Jerarquia de filtros que definio el negocio el 2026-08-07: el filtro MAYOR es
# OPERADORES (activos/apagados, baja logica); con los activos puestos viene el
# AMBIENTE. El problema que resuelve: hoy los KPIs, los cuadros y los chats mezclan
# todas las audiencias y no se sabe de que es cada numero.
#
# POR QUE CUATRO Y NO DOS. La idea inicial era fundir todo lo que no es jugador en
# `agente`. Medido sobre la copia de prod (2026-08-07) eso no se puede:
#   jugador          47.029 sesiones ·  40.807 comprobantes
#   agente           63.713 sesiones · 121.180 comprobantes (71,7% del total)
#   sin_clasificar   17.780 sesiones ·   6.932 comprobantes
# `sin_clasificar` esta dominado por la cola vacia (16.910 sesiones), que el docstring
# de arriba llama "uso interno entre operadores" pero que NO lo es: el 90% tiene
# mensajes de cliente reales y arrastra 6.795 comprobantes. Fundirla en `agente` le
# atribuiria esos depositos a los agentes, o sea exactamente el error de origen que el
# switch viene a corregir. Queda como cuarto ambiente, VISIBLE y excluido de los otros
# dos, hasta que se triaje que hay adentro.
AMBIENTES: tuple[str, ...] = ("todos", "jugador", "agente", "sin_clasificar")

# Universo canonico de segmentos (el enum de `Segment`, arriba). `todos` se define
# contra ESTA lista y no contra la union de los baldes, para que agregar un segmento
# sin asignarle ambiente rompa un test en vez de hacer desaparecer filas en silencio.
ALL_SEGMENTS: tuple[str, ...] = (
    "jugador", "agente", "marketing", "interno", "descartar", "otro",
)

# Composicion de cada ambiente. Tiene que PARTICIONAR ALL_SEGMENTS (sin solapes, sin
# huecos): con solapes `todos` contaria doble, con huecos perderia filas.
_AMBIENTE_SEGMENTS: dict[str, tuple[str, ...]] = {
    "jugador": ("jugador",),
    "agente": ("agente",),
    "sin_clasificar": ("interno", "marketing", "otro", "descartar"),
}


def segments_for_ambiente(ambiente: str) -> tuple[str, ...]:
    """Segmentos que componen un ambiente. 'todos' devuelve el universo entero.

    Falla con ValueError ante un ambiente desconocido: degradar un typo del query
    param a 'todos' abriria el tablero completo haciendole creer al que mira que esta
    viendo una audiencia sola. Un error es mejor que un numero equivocado.
    """
    if ambiente == "todos":
        return ALL_SEGMENTS
    try:
        return _AMBIENTE_SEGMENTS[ambiente]
    except KeyError:
        raise ValueError(
            f"ambiente desconocido: {ambiente!r} (validos: {list(AMBIENTES)})"
        ) from None


def ambiente_for_segment(segment: str) -> str:
    """Ambiente al que pertenece un segmento (el inverso de segments_for_ambiente).

    Nunca devuelve 'todos': ese no es el ambiente de nadie, es el union de los tres.
    """
    for amb, segs in _AMBIENTE_SEGMENTS.items():
        if segment in segs:
            return amb
    raise ValueError(
        f"segmento sin ambiente asignado: {segment!r} (conocidos: {list(ALL_SEGMENTS)})"
    )


def ambiente_incluye_sin_cola(ambiente: str) -> bool:
    """Si el ambiente abarca las conversaciones SIN cola (`queue_id IS NULL`).

    Los cuadros de /api/charts filtran por `queue_id = ANY(...)`, y en la BD NO existe
    ninguna cola de nombre vacio: las 16.910 sesiones de "cola vacia" son conversaciones
    con `queue_id IS NULL` (10.939 conversaciones). O sea que por lista de colas son
    INALCANZABLES, y el ambiente que las contiene necesita ademas un
    `OR queue_id IS NULL`. La señal se deriva de la clasificacion misma —
    segment_for_queue(None) == 'interno' — para no tener dos fuentes de verdad.
    """
    return segment_for_queue(None) in segments_for_ambiente(ambiente)


# --- QUIEN ESTA DEL OTRO LADO: persona o GRUPO ----------------------------------------
# Las personas de la copia tienen numeros de 7 a 13 digitos; los grupos, de 18 a 23. Nadie
# vive entre 14 y 17, asi que el umbral cae en un hueco limpio y no en un borde.
LARGO_MAXIMO_DE_PERSONA = 15


def es_grupo_de_whatsapp(is_group: bool | None, numero: str | None) -> bool:
    """Es un GRUPO de WhatsApp? `tickets.is_group` manda; el numero es el respaldo.

    POR QUE HAY RESPALDO. El skip `grupo_de_whatsapp` (src/router.decide_eligibility) vive
    de `tickets.is_group`, y esa marca no siempre esta. Medido sobre las 17.041 sesiones
    pendientes SIN COLA de la copia: 608 la traen en `true`, 16.168 la traen NULL -- y de
    esas 214 tienen numero de grupo. El resto (13.052) no tiene contacto y de esas no se
    puede saber nada: ahi se devuelve False, que es fallar del lado seguro. Saltear "por
    las dudas" seria borrarlas del padron sin evidencia.

    LA SEÑAL ES EXACTA, no una heuristica. WhatsApp identifica un grupo con un JID que
    viaja en `contacts.number` (ej. `120363217408052038`). Medido sobre las 93.705
    conversaciones con ticket:
        is_group = true    5.060 -> 5.060 con numero de 18-23 digitos  (100% cubierto)
        is_group = false  88.645 ->     0 con numero de mas de 15      (0 falsos positivos)
    Los JID viejos son `telefono-timestamp` (255 de los grupos tienen guion) y entran por
    el mismo umbral: 23 caracteres. No hace falta una regla propia para ellos.

    Los separadores no cuentan: un numero formateado ("+593 98 765 4321") no puede volverse
    grupo por los espacios.
    """
    if is_group is not None:
        return bool(is_group)
    limpio = "".join((numero or "").split())
    return len(limpio) > LARGO_MAXIMO_DE_PERSONA
