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


# --- LA MEDICION SE COMPARA A LA PRECISION DEL OBJETIVO --------------------------------
# EL DAÑO, MEDIDO en la copia del 2026-08-24 sobre las 380 filas deterministas: dos
# operadores perdieron DOS ESTRELLAS por 365 y 312 milisegundos.
#   17e15c5c (Christian): espera 60,365 s contra AGIL de 60 s -> `60,365 > 60` -> 3 estrellas.
#   Tenia `acredito=true` y `pregunto_algo_mas=true`: por contrato es 5.
# Y la fila no se podia verificar: `src/deposito.py` persiste `int(60,365) = 60`, asi que el
# tablero mostraba "60 s" al lado de un texto que decia "tardó 1 minuto... el objetivo es 1
# minuto" y le bajaba dos estrellas. La frase se contradecia sola.
#
# LA CAUSA es un choque de precisiones: el manual fija los objetivos en MINUTOS ENTEROS y el
# codigo los comparaba contra timestamps de WhatsApp con MILISEGUNDOS. Medir en milisegundos
# una vara escrita en minutos no es rigor, es medir ruido.
#
# SE ARREGLA ACA y no en cada rubrica: `espera_efectiva` es el punto unico por donde pasan las
# siete (promo, agilidad, soporte, retiro, info, deposito, registro). Redondeando aca, el
# numero que se COMPARA, el que se PERSISTE y el que se ESCRIBE en el texto son el mismo.
# NO BAJA LA VARA: solo alcanza a lo que estaba a menos de medio segundo del umbral. Medido:
# 2 filas de 380 (0,5%). Una tolerancia de 30 s habria movido el 12%, y eso si seria otra vara.

def test_la_espera_se_redondea_a_segundos_enteros():
    from datetime import datetime, timedelta, timezone

    from src.horario import espera_efectiva

    # Un martes 10:00 en horario de atencion, para que el recorte no interfiera.
    desde = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    for extra, esperado in ((0.365, 60), (0.312, 60), (0.6, 61), (0.0, 60)):
        td = espera_efectiva(desde, desde + timedelta(seconds=60 + extra))
        assert td.total_seconds() == float(esperado), (
            f"60+{extra}s deberia medir {esperado}s y midio {td.total_seconds()}")
        assert td.microseconds == 0, "quedaron microsegundos en la medicion"


def test_el_caso_real_deja_de_exceder_el_objetivo():
    """`60,365 > 60` era True y costaba dos estrellas; a la precision del objetivo, no excede."""
    from datetime import datetime, timedelta, timezone

    from src.horario import espera_efectiva

    desde = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    medida = espera_efectiva(desde, desde + timedelta(seconds=60.365))
    assert not (medida > timedelta(minutes=1)), "sigue excediendo el objetivo por milisegundos"


def test_medio_segundo_de_mas_SI_excede():
    """El redondeo no puede volverse una tolerancia: mas de medio segundo sigue siendo tarde."""
    from datetime import datetime, timedelta, timezone

    from src.horario import espera_efectiva

    desde = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    medida = espera_efectiva(desde, desde + timedelta(seconds=60.9))
    assert medida > timedelta(minutes=1)


def test_las_puntas_faltantes_y_el_cero_no_cambian():
    from datetime import datetime, timedelta, timezone

    from src.horario import espera_efectiva

    d = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    assert espera_efectiva(None, d) is None
    assert espera_efectiva(d, None) is None
    assert espera_efectiva(d, d) == timedelta(0)
    assert espera_efectiva(d, d - timedelta(seconds=5)) == timedelta(0)
