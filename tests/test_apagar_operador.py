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


# --- LA OTRA MITAD: LA ESCRITURA (2026-08-27) --------------------------------------
# El arreglo de arriba toco los cuatro puntos de LECTURA y dejo `_APPLY` — la ESCRITURA —
# comparando por string exacto. Medido en produccion el 2026-08-27:
#
#   operator_status tiene 'RAMIREZ' (sembrado el 06-ago del dump del 04-ago).
#   El GET resuelve por users.name y devuelve 'Ramirez'. El modal manda ESE nombre.
#   ON CONFLICT (account, operator_name) NO matchea -> INSERTA UNA SEGUNDA FILA.
#
# Y como la lectura hace `coalesce(bool_and(os.activo), true)` sobre el join por clave,
# matchea las DOS filas: false AND true = false. El apagado gana para siempre.
#
# Sintoma exacto que se vio: PUT /api/operators -> 200 {"actualizados": 1} y el operador
# NO se prende. Nunca. Ramirez tenia 222 sesiones recientes y actividad ese mismo dia.

def test_la_ESCRITURA_tambien_compara_por_clave():
    """El bug no es que falle: es que devuelve 200 y no hace nada. Peor que un error."""
    from src.operators_status import _APPLY
    assert "translate(lower(operator_name)" in _APPLY, (
        "_APPLY sigue matcheando por string exacto: 'Ramirez' no encuentra a 'RAMIREZ' "
        "y crea una fila duplicada que el bool_and convierte en un apagado permanente"
    )


def test_apply_actualiza_la_fila_que_ya_existe_en_vez_de_insertar_otra():
    """`ON CONFLICT` sobre la PK exacta no sirve: la PK es el string y la comparacion es
    la clave. Tiene que ACTUALIZAR lo que matchee por clave, e INSERTAR solo si no habia
    nada."""
    from src.operators_status import _APPLY
    sql = " ".join(_APPLY.split()).upper()
    assert "UPDATE OPERATOR_STATUS" in sql, "_APPLY no actualiza la fila existente"
    assert "WHERE NOT EXISTS" in sql, (
        "_APPLY inserta siempre; tiene que insertar SOLO si no matcheo ninguna fila"
    )


def test_apply_normaliza_las_dos_filas_duplicadas_que_ya_existen():
    """Las filas duplicadas ya estan en produccion ('RAMIREZ' y la que dejo el modal). El
    UPDATE tiene que alcanzarlas a TODAS, porque si deja una en false el bool_and la sigue
    apagando."""
    from src.operators_status import _APPLY
    cuerpo = _APPLY.upper().split("WHERE NOT EXISTS")[0]
    assert "LIMIT" not in cuerpo, "_APPLY actualiza una sola fila y deja la otra apagando"


def test_los_CINCO_puntos_de_matcheo_usan_la_clave():
    """Cuatro de lectura y uno de escritura. Faltaba el de escritura y por eso la
    configuracion no se podia arreglar desde la UI."""
    from src.queries import _SIN_APAGADOS, _SIN_APAGADOS_CHARTS, _SIN_APAGADOS_CONV
    from src.operators_status import _ADMIN_ROWS, _APPLY
    for nombre, sql in (("_SIN_APAGADOS", _SIN_APAGADOS),
                        ("_SIN_APAGADOS_CHARTS", _SIN_APAGADOS_CHARTS),
                        ("_SIN_APAGADOS_CONV", _SIN_APAGADOS_CONV),
                        ("_ADMIN_ROWS", _ADMIN_ROWS),
                        ("_APPLY", _APPLY)):
        assert "translate(lower(operator_name)" in sql or \
               "translate(lower(os.operator_name)" in sql, \
               f"{nombre} compara por string exacto"


def test_el_SEED_no_vuelve_a_sembrar_la_grafia_vieja_en_cada_arranque():
    """Arreglar solo `_APPLY` no alcanza. `seed_from_config` corre en CADA arranque del
    contenedor con `ON CONFLICT (account, operator_name) DO NOTHING`: si el archivo dice
    'RAMIREZ' y la tabla ya tiene 'Ramirez', la PK exacta no matchea y el seed RE-INSERTA
    la fila apagada. O sea que el proximo deploy deshace el arreglo hecho desde la UI."""
    from src.operators_status import _SEED
    assert "translate(lower(operator_name)" in _SEED, (
        "_SEED matchea por string exacto: cada deploy vuelve a insertar la grafia vieja "
        "y el bool_and vuelve a apagar al operador"
    )


def test_REACTIVAR_compara_por_clave_al_decidir_a_quien_deja_apagado():
    """`--pisar` deja apagados a los que el archivo nombra. Si compara por string exacto,
    una grafia distinta lo saca de la lista de 'mantener' y lo PRENDE sin que nadie lo
    haya pedido — el error simetrico del de arriba, y igual de silencioso."""
    from src.operators_status import _REACTIVAR
    assert "translate(lower(operator_name)" in _REACTIVAR, (
        "_REACTIVAR compara por string exacto: una grafia distinta prende a alguien que "
        "el archivo manda tener apagado"
    )


def test_los_TRES_puntos_de_ESCRITURA_usan_la_clave():
    """El resumen: seed (arranque), apply (modal y --pisar) y reactivar (--pisar)."""
    import src.operators_status as m
    for nombre in ("_SEED", "_APPLY", "_REACTIVAR"):
        assert "translate(lower(operator_name)" in getattr(m, nombre), \
            f"{nombre} escribe comparando por string exacto"
