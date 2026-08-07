"""Apagar un operador tiene que funcionar aunque el nombre se escriba distinto.

EL BUG QUE ESTO EVITA. `operator_status` matcheaba por STRING EXACTO. Cuando la
canonicalizacion por persona (2026-08-07) eligio la grafia dominante, dos apagados
quedaron sin efecto:

    [datos]    'OnlySorti' guardado  ->  'onlysorti' resuelto   ** no matchea **
    [sistemas] 'Anahi'     guardado  ->  'Anahí'     resuelto   ** no matchea **

Anahí tiene **31.626 mensajes** y estaba apagada: con el match roto volvia a aparecer en
todos los cuadros sin que nadie tocara nada. La configuracion se rompe sola cada vez que
cambia una grafia, y el sintoma (un operador que reaparece) no apunta a la causa.

Se compara por CLAVE (minusculas, sin tildes) en vez de renombrar las filas: renombrar
arregla estos dos y deja la trampa armada para el proximo.
"""
from src.queries import _clave_sql


def test_la_comparacion_ignora_mayusculas():
    assert "lower(" in _clave_sql("x")


def test_la_comparacion_ignora_tildes():
    # translate() y no unaccent(): unaccent es una EXTENSION que puede no estar instalada
    # en la base, y esto tiene que andar en produccion sin pedir permisos de superusuario.
    expr = _clave_sql("x")
    assert "translate(" in expr
    for vocal in "áéíóú":
        assert vocal in expr


def test_envuelve_la_expresion_que_le_pasan():
    assert "coalesce(u.name, cs.user_name)" in _clave_sql("coalesce(u.name, cs.user_name)")


def test_los_cuatro_puntos_de_matcheo_usan_la_clave():
    """Si uno solo compara por string exacto, el operador queda apagado en unas tarjetas
    y encendido en otras — que es peor que no apagarlo, porque los numeros no cierran."""
    from src.queries import _SIN_APAGADOS, _SIN_APAGADOS_CHARTS, _SIN_APAGADOS_CONV
    from src.operators_status import _ADMIN_ROWS
    for nombre, sql in (("_SIN_APAGADOS", _SIN_APAGADOS),
                        ("_SIN_APAGADOS_CHARTS", _SIN_APAGADOS_CHARTS),
                        ("_SIN_APAGADOS_CONV", _SIN_APAGADOS_CONV),
                        ("_ADMIN_ROWS", _ADMIN_ROWS)):
        assert "translate(lower(os.operator_name)" in sql, f"{nombre} compara por string exacto"
