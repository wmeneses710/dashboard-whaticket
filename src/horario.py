"""Horario de atencion y espera EFECTIVA (la que corre con alguien trabajando).

Fuente unica del horario del negocio. Vivia dentro de src/agilidad.py, que lo usaba solo
para el segmento agente; se saco aca cuando quedo claro que TODAS las rubricas que miden un
reloj lo necesitan.

POR QUE. MEDIDO el 2026-08-07: de 50 sesiones con 1-2 estrellas, **13 (26 por ciento)** eran
clientes que escribieron de madrugada y operadores que contestaron ni bien abrio el turno.
El tablero les reprochaba:

    cliente 04:25 -> operador 06:08   "Respondió recién 1,7 horas después"   (fueron 8 min)
    cliente 03:52 -> operador 06:06   "2,2 horas después"                    (fueron 6 min)
    cliente 00:30 -> operador 06:04   (registro)

A las 04:00 no hay nadie trabajando. Ese tiempo no es una demora del operador, es la noche,
y calificarlo asi es castigar a alguien por el reloj del cliente.

La operacion corre 06:00-23:59 hora de Ecuador (regla del negocio confirmada el 2026-08-07).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Ecuador no tiene horario de verano, asi que un offset fijo es exacto y no depende de que
# la base de datos de zonas horarias del sistema este al dia.
TZ = timezone(timedelta(hours=-5))

HORA_ABRE = 6     # 06:00 abre
HORA_CIERRA = 23  # el ultimo tramo es 23:00-23:59; a las 00:00 ya esta cerrado

_APERTURA = timedelta(hours=HORA_ABRE)
_CIERRE = timedelta(hours=HORA_CIERRA + 1)          # 24:00 = fin del dia operativo
_POR_DIA = _CIERRE - _APERTURA                       # 18 h de atencion por dia


def en_horario(cuando: datetime) -> bool:
    """El instante cae dentro del horario de atencion (hora local de Ecuador)."""
    return HORA_ABRE <= cuando.astimezone(TZ).hour <= HORA_CIERRA


def _recortar(cuando: datetime) -> tuple[datetime, timedelta]:
    """(dia operativo, offset dentro del horario) para un instante cualquiera.

    Un instante ANTES de abrir cuenta como la apertura de ese dia; DESPUES de cerrar, como
    el cierre. Asi la resta entre dos instantes recortados ya descuenta la noche.
    """
    local = cuando.astimezone(TZ)
    dia = local.replace(hour=0, minute=0, second=0, microsecond=0)
    desde_medianoche = local - dia
    if desde_medianoche < _APERTURA:
        return dia, _APERTURA
    if desde_medianoche > _CIERRE:
        return dia, _CIERRE
    return dia, desde_medianoche


def espera_efectiva(desde: datetime | None, hasta: datetime | None) -> timedelta | None:
    """Tiempo transcurrido contando SOLO el horario de atencion.

    None si falta cualquiera de las dos puntas (hay caminos sin timestamps).
    timedelta(0) si `hasta` no es posterior, o si todo el tramo cae con el negocio cerrado.
    """
    if desde is None or hasta is None:
        return None
    if hasta <= desde:
        return timedelta(0)
    dia_a, off_a = _recortar(desde)
    dia_b, off_b = _recortar(hasta)
    dias = (dia_b - dia_a).days
    return max(timedelta(0), _POR_DIA * dias + (off_b - off_a))
