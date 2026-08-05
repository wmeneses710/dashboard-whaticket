"""Integridad de la atribucion de messages.conversation_id, aplicada a la sesionizacion.

POR QUE EXISTE ESTE MODULO. La sesionizacion mide el gap por INACTIVIDAD real: el
silencio entre el ultimo mensaje del episodio previo (last_at) y el primer mensaje del
siguiente (first_at). Esos dos valores salen de `messages` agrupado por
`conversation_id` (ver _LAST_MSG_SQL en src/sessions.py) — exactamente la columna que
el ETL atribuye mal en una fraccion grande de los mensajes de la cuenta `sistemas`.

Es el MISMO dato por el que el SPAN se dejo deliberadamente sobre created_at. Si el gap
se apoya en el, hay que poder responder con numeros: cada decision que cambio al pasar a
inactividad, cambio por actividad REAL o por mensajes que no pertenecen a ese episodio?

La direccion del error importa. Si un episodio previo absorbio mensajes posteriores a su
resolucion, su last_at queda adelantado, el silencio sale muy negativo, nunca supera GAP
y la frontera MERGEA. Eso reintroduce el over-merge que la regla de cierre habia
resuelto, por otra puerta. Ese es el riesgo que estas funciones cuantifican.

Todo aca es PURO: recibe timestamps, devuelve clasificacion. El acceso a la base y la
simulacion de las dos reglas viven en scripts/sim_sesiones_gap.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.sessions import CLOSING, GAP, _actividad, _fin_de_actividad, assign_sessions

# Direcciones del desvio de un timestamp respecto de la ventana propia de su episodio.
ANTES = "antes"      # el mensaje es anterior al nacimiento de su conversacion
DESPUES = "despues"  # el mensaje es posterior a la resolucion de su conversacion

# Margen por defecto. Un mensaje puede llegar unos segundos antes de que se inserte la
# fila de la conversacion, o unos segundos despues de que se marque resuelta: eso es
# ruido de escritura, no mala atribucion. La patologia real se mide en dias.
TOLERANCIA = timedelta(minutes=5)


@dataclass(frozen=True)
class Desvio:
    """Cuanto y hacia donde se sale un timestamp de la ventana propia de su episodio."""
    direccion: str  # ANTES | DESPUES
    magnitud: timedelta


@dataclass(frozen=True)
class Frontera:
    """Una frontera entre dos episodios consecutivos del mismo ticket.

    gap_viejo = ep.created_at - prev.created_at   (regla vieja: nacimiento a nacimiento)
    gap_nuevo = ep.first_at - min(prev.last_at, prev.resolved_at)   (regla nueva: silencio
                real, con el techo de _fin_de_actividad)

    El desvio se sigue marcando aunque el techo neutralice su efecto: el dato ESTA mal
    atribuido, y saber cuantas fronteras dependian de el es justamente la medicion.

    prev_cerro / cambio_operador son IGUALES en las dos reglas: no dependen de como se
    mide el gap. Se guardan porque cuando alguno es verdadero la frontera corta con
    cualquier gap, y entonces el termino gap no decide nada (ver es_flip_de_gap).

    desvio_prev = desvio del last_at del previo respecto de la ventana del PREVIO.
    desvio_ep   = desvio del first_at del siguiente respecto de la ventana del SIGUIENTE.
    None = ese timestamp cae dentro de su ventana (o no hay timestamp que auditar).
    """
    ticket_id: object
    prev_id: object
    ep_id: object
    gap_viejo: timedelta
    gap_nuevo: timedelta
    prev_cerro: bool
    cambio_operador: bool
    desvio_prev: Desvio | None
    desvio_ep: Desvio | None


@dataclass(frozen=True)
class Comparacion:
    """Efecto de correr las DOS reglas sobre los mismos episodios.

    Dos formas de contar los episodios que se mueven, porque miden cosas distintas y
    conviene no confundirlas:

    cambio_session_id  = el episodio quedo colgado de otro session_id. Es la cuenta
                         "chica": cuando una sesion {a,b} se parte en {a} y {b}, solo b
                         cambia de session_id (a se queda con el suyo).
    sesion_recompuesta = cambio el CONJUNTO de episodios que comparten su sesion. Es la
                         cuenta "grande" y la que importa para el scoring: a TAMBIEN se
                         re-scorea, porque su transcript mergeado ya no es el mismo.
    """
    tickets: int
    episodios: int
    sesiones_vieja: int
    sesiones_nueva: int
    cambio_session_id: int
    sesion_recompuesta: int
    fronteras: list


@dataclass(frozen=True)
class Resumen:
    """Los dos baldes, ya contados."""
    fronteras: int
    flips: int
    corta_a_mergea: int
    mergea_a_corta: int
    limpios: int
    sucios: int
    sucios_por_prev_futuro: int   # last_at del previo posterior a su resolucion
    sucios_por_ep_pasado: int     # first_at del siguiente anterior a su nacimiento
    sucios_por_ambos: int
    # DOS RATIOS, DOS PREGUNTAS DISTINTAS. No confundirlos:
    # pct_sucios (sobre los FLIPS) = CALIDAD del fix: de los cambios que introduce,
    #   que fraccion la mueven datos mal atribuidos. Con pocos flips es ruidoso.
    # pct_sucios_sobre_fronteras = RADIO DE IMPACTO: cuantas fronteras del universo
    #   quedarian mal si se re-materializa. Este es el que manda el go/no-go.
    pct_sucios: float
    pct_sucios_sobre_fronteras: float


def desvio_de_ventana(
    t: datetime | None,
    creado_en: datetime,
    resuelto_en: datetime | None,
    tolerancia: timedelta = TOLERANCIA,
) -> Desvio | None:
    """Cuanto se sale `t` de la ventana propia del episodio [creado_en, resuelto_en].

    `t` es un timestamp de MENSAJE atribuido a ese episodio. Si cae fuera de la ventana
    de vida del episodio, el mensaje no puede ser suyo: esta mal atribuido.

    t None -> None: el episodio no tiene mensajes reales, el gap cae a created_at por el
    fallback de _actividad y no hay nada que auditar.

    resuelto_en None -> episodio ABIERTO: no hay techo, cualquier mensaje posterior es
    legitimo. Solo se audita el piso.
    """
    if t is None:
        return None
    if t < creado_en - tolerancia:
        return Desvio(direccion=ANTES, magnitud=creado_en - t)
    if resuelto_en is not None and t > resuelto_en + tolerancia:
        return Desvio(direccion=DESPUES, magnitud=t - resuelto_en)
    return None


def es_flip_de_gap(f: Frontera, gap: timedelta = GAP) -> bool:
    """El termino gap, y solo el, cambio la decision de cortar/mergear en esta frontera.

    Dos condiciones:
    1. Ningun otro motivo fuerza el corte (el previo no cerro y no cambio el operador).
       Si alguno fuerza, la frontera corta con cualquier gap y medirlo distinto no mueve
       la aguja: no cuenta como flip.
    2. Los dos gaps caen a lados distintos de GAP.

    OJO — el SPAN queda AFUERA a proposito. span_exceeded depende de session_start, que
    depende de todas las decisiones anteriores de la cadena, y esa cadena difiere entre
    las dos reglas. No se puede atribuir a una frontera aislada. El efecto total
    (incluido el span y las cascadas) se mide corriendo assign_sessions completa sobre
    el ticket, no aca.
    """
    if f.prev_cerro or f.cambio_operador:
        return False
    return (f.gap_viejo > gap) != (f.gap_nuevo > gap)


def es_sucia(f: Frontera) -> bool:
    """El flip tiene algun timestamp que no pertenece a su episodio.

    CONSERVADOR A PROPOSITO desde que existe el techo de _fin_de_actividad. `desvio_prev`
    se sigue reportando, pero ese valor YA NO alimenta el gap: el techo lo recorta antes.
    O sea, una frontera marcada sucia por `desvio_prev` tiene el dato podrido al lado, no
    adentro de la decision. El unico desvio que todavia puede torcer una frontera es
    `desvio_ep` (first_at del siguiente anterior a su nacimiento), porque ese lado no
    lleva techo.

    Se deja asi —sobre-contando— porque para un go/no-go conviene errar por pesimista: el
    numero real de fronteras decididas por dato podrido es <= el que reporta esta funcion.
    Si algun dia hace falta precision, separar en "sucia_que_decide" (solo desvio_ep) y
    "sucia_adyacente" (desvio_prev).
    """
    return f.desvio_prev is not None or f.desvio_ep is not None


def resumen(fronteras: list[Frontera], gap: timedelta = GAP) -> Resumen:
    """Cuenta los flips y los reparte en los dos baldes: LIMPIO y SUCIO.

    LIMPIO = la decision cambio por actividad real -> el fix hace lo que promete.
    SUCIO  = la decision cambio por mensajes mal atribuidos -> el fix esta horneando el
             bug del ETL en las fronteras de sesion.
    """
    flips = [f for f in fronteras if es_flip_de_gap(f, gap)]
    sucios = [f for f in flips if es_sucia(f)]
    prev_futuro = [f for f in sucios
                   if f.desvio_prev is not None and f.desvio_prev.direccion == DESPUES]
    ep_pasado = [f for f in sucios
                 if f.desvio_ep is not None and f.desvio_ep.direccion == ANTES]
    ambos = [f for f in sucios if f.desvio_prev is not None and f.desvio_ep is not None]
    # corta->mergea: la vieja superaba GAP y la nueva no. Sobre los flips, la direccion
    # queda determinada por gap_viejo (si flipeo y la vieja cortaba, la nueva mergea).
    corta_a_mergea = sum(1 for f in flips if f.gap_viejo > gap)
    return Resumen(
        fronteras=len(fronteras),
        flips=len(flips),
        corta_a_mergea=corta_a_mergea,
        mergea_a_corta=len(flips) - corta_a_mergea,
        limpios=len(flips) - len(sucios),
        sucios=len(sucios),
        sucios_por_prev_futuro=len(prev_futuro),
        sucios_por_ep_pasado=len(ep_pasado),
        sucios_por_ambos=len(ambos),
        pct_sucios=(100.0 * len(sucios) / len(flips)) if flips else 0.0,
        pct_sucios_sobre_fronteras=(
            (100.0 * len(sucios) / len(fronteras)) if fronteras else 0.0),
    )


# Ancla en las queries de mensajes de src/sessions.py donde se inyecta el piso. Si algun
# dia cambia ese WHERE, con_piso_de_mensajes revienta con un error claro en vez de
# devolver un SQL sin piso, que mediria el mundo equivocado sin avisar.
_ANCLA = "WHERE account = %(account)s"
_PISO = "WHERE account = %(account)s\n   AND created_at >= COALESCE(%(msgs_desde)s, '-infinity'::timestamptz)"


def con_piso_de_mensajes(sql: str) -> str:
    """Agrega un piso de fecha sobre `messages.created_at` a una query de src/sessions.py.

    PARA QUE. La decision de scope (retener solo desde una fecha) se aplica a los
    MENSAJES, no a las conversaciones: en `sistemas` todas las conversaciones ya estan
    dentro de la ventana, y lo que sobra son mensajes viejos pegados a conversaciones
    nuevas. Para simular el mundo post-recorte hay que hacer que las queries no vean esos
    mensajes — las TRES, porque despues del recorte no van a existir y afectan tanto la
    ventana de actividad como el ultimo body y el operador dominante.

    El valor va como PARAMETRO (`msgs_desde`), no interpolado: None -> '-infinity' -> la
    query se comporta como la original.
    """
    if _ANCLA not in sql:
        raise ValueError(
            f"no se encontro el ancla {_ANCLA!r} en la query: cambio src/sessions.py y "
            "el piso de mensajes no se puede inyectar sin revisar el SQL a mano")
    return sql.replace(_ANCLA, _PISO, 1)


@dataclass(frozen=True)
class Filtrado:
    """Resultado de recortar los tickets a una ventana de fechas."""
    tickets: dict
    tickets_excluidos: int
    episodios_excluidos: int


def filtrar_tickets_completos(
    por_ticket: dict, desde: datetime | None, hasta: datetime | None,
) -> Filtrado:
    """Deja solo los tickets cuyos episodios caen ENTEROS en [desde, hasta] (inclusive).

    PARA QUE. El re-heal del ETL avanza por tramos de tiempo, asi que en una copia a
    medio sanar conviven dos poblaciones: la ya sanada y la vieja. Medir las dos juntas
    da un promedio que no contesta nada; medirlas por separado contesta si el re-heal
    arregla la atribucion.

    POR QUE EL TICKET COMPLETO Y NO EL EPISODIO. La unidad de analisis es el ticket: las
    fronteras son pares de episodios consecutivos SUYOS. Si se recortaran episodios por
    la mitad de un ticket, el primer episodio que queda pareceria inicio de sesion y se
    fabricarian fronteras que no existen. Un ticket a caballo del borde se excluye entero
    y se CUENTA (el reporte lo imprime: nada se descarta en silencio).

    desde/hasta None = ese lado queda abierto. Se filtra por created_at del episodio,
    que es el nacimiento y no se mueve; usar la actividad aca seria circular, porque la
    actividad es justamente el dato cuya integridad se esta auditando.
    """
    if desde is None and hasta is None:
        return Filtrado(tickets=por_ticket, tickets_excluidos=0, episodios_excluidos=0)

    dentro: dict = {}
    excluidos = 0
    eps_excluidos = 0
    for ticket_id, eps in por_ticket.items():
        creados = [ep["created_at"] for ep in eps]
        if (desde is None or min(creados) >= desde) and \
                (hasta is None or max(creados) <= hasta):
            dentro[ticket_id] = eps
        else:
            excluidos += 1
            eps_excluidos += len(eps)
    return Filtrado(tickets=dentro, tickets_excluidos=excluidos,
                    episodios_excluidos=eps_excluidos)


def sin_actividad(episodios: list[dict]) -> list[dict]:
    """Los mismos episodios SIN first_at/last_at -> assign_sessions cae a created_at.

    Asi se reproduce la regla VIEJA con la funcion REAL, sin reimplementarla: si algun
    dia cambia assign_sessions, la simulacion cambia con ella y no queda una copia
    desincronizada de la regla. No muta la entrada.
    """
    return [{k: v for k, v in ep.items() if k not in ("first_at", "last_at")}
            for ep in episodios]


def ordenar_episodios(episodios: list[dict]) -> list[dict]:
    """Mismo orden que usa assign_sessions internamente: (created_at, str(id))."""
    return sorted(episodios, key=lambda e: (e["created_at"], str(e["conversation_id"])))


def fronteras_de_ticket(
    ticket_id, episodios: list[dict], tolerancia: timedelta = TOLERANCIA,
) -> list[Frontera]:
    """Una Frontera por par de episodios consecutivos del ticket.

    Cada episodio necesita created_at, resolved_at, first_at, last_at,
    last_operator_body y operator_id. Los desvios se auditan sobre los timestamps CRUDOS
    (first_at/last_at pueden ser None: episodio sin mensajes reales, ahi el gap cae a
    created_at y no hay atribucion que juzgar).
    """
    eps = ordenar_episodios(episodios)
    out = []
    for prev, ep in zip(eps, eps[1:]):
        a_prev, a_cur = prev.get("operator_id"), ep.get("operator_id")
        out.append(Frontera(
            ticket_id=ticket_id,
            prev_id=prev["conversation_id"],
            ep_id=ep["conversation_id"],
            gap_viejo=ep["created_at"] - prev["created_at"],
            gap_nuevo=_actividad(ep, "first_at") - _fin_de_actividad(prev),
            prev_cerro=bool(CLOSING.search(prev.get("last_operator_body") or "")),
            cambio_operador=(a_prev is not None and a_cur is not None and a_prev != a_cur),
            desvio_prev=desvio_de_ventana(
                prev.get("last_at"), prev["created_at"], prev.get("resolved_at"),
                tolerancia),
            desvio_ep=desvio_de_ventana(
                ep.get("first_at"), ep["created_at"], ep.get("resolved_at"), tolerancia),
        ))
    return out


def particion_por_sesion(asignacion: list[dict]) -> dict:
    """conversation_id -> frozenset de los episodios que comparten su sesion."""
    grupos: dict = defaultdict(set)
    for a in asignacion:
        grupos[a["session_id"]].add(a["conversation_id"])
    return {a["conversation_id"]: frozenset(grupos[a["session_id"]]) for a in asignacion}


def comparar_reglas(
    por_ticket: dict, tolerancia: timedelta = TOLERANCIA,
) -> Comparacion:
    """Corre las DOS reglas por ticket y acumula el efecto total + todas las fronteras.

    por_ticket: ticket_id -> lista de episodios (ver fronteras_de_ticket).

    El efecto total sale de assign_sessions COMPLETA, asi que incluye el span y las
    cascadas; los flips por frontera (resumen) miran solo el termino gap. Los dos numeros
    contestan preguntas distintas y no tienen por que coincidir.
    """
    episodios = sesiones_vieja = sesiones_nueva = 0
    cambio_session_id = sesion_recompuesta = 0
    fronteras: list[Frontera] = []

    for ticket_id, eps in por_ticket.items():
        eps_ord = ordenar_episodios(eps)
        nueva = assign_sessions(eps_ord)
        vieja = assign_sessions(sin_actividad(eps_ord))
        episodios += len(eps_ord)
        sesiones_nueva += len({a["session_id"] for a in nueva})
        sesiones_vieja += len({a["session_id"] for a in vieja})

        sid_nueva = {a["conversation_id"]: a["session_id"] for a in nueva}
        sid_vieja = {a["conversation_id"]: a["session_id"] for a in vieja}
        cambio_session_id += sum(1 for c in sid_nueva if sid_nueva[c] != sid_vieja[c])
        if sid_nueva != sid_vieja:  # solo pagar la particion si el ticket cambio
            p_nueva = particion_por_sesion(nueva)
            p_vieja = particion_por_sesion(vieja)
            sesion_recompuesta += sum(1 for c in p_nueva if p_nueva[c] != p_vieja[c])

        if len(eps_ord) > 1:
            fronteras.extend(fronteras_de_ticket(ticket_id, eps_ord, tolerancia))

    return Comparacion(
        tickets=len(por_ticket),
        episodios=episodios,
        sesiones_vieja=sesiones_vieja,
        sesiones_nueva=sesiones_nueva,
        cambio_session_id=cambio_session_id,
        sesion_recompuesta=sesion_recompuesta,
        fronteras=fronteras,
    )


def percentiles(valores: list[timedelta]) -> dict:
    """p50/p90/max de una lista de magnitudes. {} si esta vacia.

    Para leer la escala del desvio: si los sucios se salen por minutos es ruido; si se
    salen por meses, es la patologia del ETL.
    """
    if not valores:
        return {}
    ordenados = sorted(valores)
    def _p(q):
        # Percentil por posicion (nearest-rank): sin numpy y determinista.
        idx = min(len(ordenados) - 1, int(q * len(ordenados)))
        return ordenados[idx]
    return {"p50": _p(0.50), "p90": _p(0.90), "max": ordenados[-1]}
