"""La espera se mide en HORARIO DE ATENCION, no en tiempo de reloj.

MEDIDO el 2026-08-07: de 50 sesiones con 1-2 estrellas, **13 (26 por ciento)** eran clientes
que escribieron FUERA de horario y operadores que contestaron ni bien abrio el turno. El
tablero les decia cosas como:

    cliente 04:25 -> operador 06:08   "Respondió recién 1,7 horas después"   (fueron 8 min)
    cliente 03:52 -> operador 06:06   "2,2 horas después"                    (fueron 6 min)

Nadie estaba trabajando a las 04:00: la operacion corre 06:00-23:59 (hora de Ecuador). Ese
tiempo no es una demora del operador, es la noche.
"""
from datetime import datetime, timedelta, timezone

from src.horario import TZ, en_horario, espera_efectiva


def _ec(dia, hora, minuto=0):
    """Un instante en hora de Ecuador."""
    return datetime(2026, 8, dia, hora, minuto, tzinfo=TZ)


# --- el horario en si -------------------------------------------------------------

def test_los_bordes_del_horario():
    assert en_horario(_ec(7, 5, 59)) is False
    assert en_horario(_ec(7, 6, 0)) is True
    assert en_horario(_ec(7, 23, 59)) is True
    assert en_horario(_ec(8, 0, 0)) is False


# --- la espera efectiva ----------------------------------------------------------

def test_dentro_del_horario_es_la_diferencia_normal():
    assert espera_efectiva(_ec(7, 10, 0), _ec(7, 10, 30)) == timedelta(minutes=30)


def test_el_cliente_escribe_de_madrugada_y_el_operador_abre_el_turno():
    # El caso real: 04:25 -> 06:08. Son 8 minutos de turno, no 1,7 horas.
    assert espera_efectiva(_ec(7, 4, 25), _ec(7, 6, 8)) == timedelta(minutes=8)


def test_el_cliente_escribe_tarde_y_le_contestan_al_dia_siguiente():
    # 23:50 -> 06:10 del otro dia: 10 min de la noche anterior + 10 de la mañana.
    # OJO con el borde: HORA_CIERRA=23 significa que el ULTIMO TRAMO atendido es 23:00-23:59,
    # o sea que la ventana cierra a las 24:00 y de 23:50 al cierre hay 10 minutos, no 9.
    # Escribi 9 en el primer intento y el test fallo contra el codigo, que estaba bien.
    assert espera_efectiva(_ec(7, 23, 50), _ec(8, 6, 10)) == timedelta(minutes=20)


def test_varios_dias_cerrados_no_suman():
    # 23:30 del dia 7 -> 07:00 del dia 9: 30 min al cierre + un dia entero (18h) + 60 min.
    esperado = timedelta(minutes=30) + timedelta(hours=18) + timedelta(minutes=60)
    assert espera_efectiva(_ec(7, 23, 30), _ec(9, 7, 0)) == esperado


def test_los_dos_extremos_fuera_de_horario_dan_cero():
    assert espera_efectiva(_ec(7, 1, 0), _ec(7, 4, 0)) == timedelta(0)


def test_una_demora_REAL_dentro_del_horario_se_conserva():
    # Lo que NO hay que perdonar: el cliente escribio en horario y lo dejaron 3 horas.
    assert espera_efectiva(_ec(7, 9, 0), _ec(7, 12, 0)) == timedelta(hours=3)


def test_orden_invertido_da_cero():
    assert espera_efectiva(_ec(7, 12, 0), _ec(7, 10, 0)) == timedelta(0)


def test_tolera_UTC_y_convierte():
    # Los timestamps de la BD vienen en UTC; 09:25 UTC = 04:25 en Ecuador (fuera de horario).
    desde = datetime(2026, 8, 7, 9, 25, tzinfo=timezone.utc)
    hasta = datetime(2026, 8, 7, 11, 8, tzinfo=timezone.utc)   # 06:08 EC
    assert espera_efectiva(desde, hasta) == timedelta(minutes=8)


def test_sin_fecha_devuelve_None():
    assert espera_efectiva(None, _ec(7, 10, 0)) is None
    assert espera_efectiva(_ec(7, 10, 0), None) is None
