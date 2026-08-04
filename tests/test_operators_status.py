"""Estado activo/inactivo de operadores (baja lógica): archivo <-> BD.

La BD es la FUENTE en runtime (viaja en la copia de prod, que es lo único que podemos bajar
del server); el archivo es el ESPEJO auditable y la semilla para un entorno nuevo.

FORMATO: el archivo lista SOLO a los APAGADOS. "Activo" es el default de la BD, así que no
es una decisión que haya que registrar — es la ausencia de una. Enumerar los 28 activos
además de los 33 apagados era redundante y, peor, abría la puerta a que las dos listas se
contradigan.
"""
import json

import pytest

from src.operators_status import (
    CONFIG_VERSION,
    apply_config,
    config_from_rows,
    config_rows,
    ensure_table,
    inactive_names,
    parse_config,
    seed_from_config,
    suggest_from_activity,
)


class _FakeCursor:
    """Cursor mínimo: registra lo ejecutado y devuelve filas preparadas."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def executemany(self, sql, seq):
        self.executed.append((sql, list(seq)))
        return self

    def fetchall(self):
        return self._rows


# --- forma del archivo ------------------------------------------------------

def test_parse_config_lee_solo_apagados_y_normaliza():
    cfg = parse_config(json.dumps({
        "version": CONFIG_VERSION,
        "apagados": {
            "sistemas": [
                {"operador": "MariCruz", "comentario": "ya no trabaja"},
                {"operador": "  Teddy  "},
            ],
        },
    }))
    ops = cfg["apagados"]["sistemas"]
    assert ops[0] == {"operador": "MariCruz", "comentario": "ya no trabaja"}
    # se recorta: un espacio de más rompería el match con el nombre resuelto
    assert ops[1] == {"operador": "Teddy", "comentario": ""}


def test_parse_config_acepta_una_lista_de_strings_a_secas():
    """Escribir el archivo a mano tiene que ser barato: una lista de nombres alcanza."""
    cfg = parse_config(json.dumps({"apagados": {"datos": ["Kathya", "OnlySorti"]}}))
    assert cfg["apagados"]["datos"] == [
        {"operador": "Kathya", "comentario": ""},
        {"operador": "OnlySorti", "comentario": ""},
    ]


def test_parse_config_rechaza_forma_invalida():
    with pytest.raises(ValueError, match="apagados"):
        parse_config(json.dumps({"version": CONFIG_VERSION}))
    with pytest.raises(ValueError, match="lista"):
        parse_config(json.dumps({"apagados": {"datos": {"operador": "Alex"}}}))
    with pytest.raises(ValueError, match="operador"):
        parse_config(json.dumps({"apagados": {"datos": [{"comentario": "x"}]}}))


def test_parse_config_rechaza_duplicado():
    with pytest.raises(ValueError, match="duplicado"):
        parse_config(json.dumps({"apagados": {"datos": ["Alex", "Alex"]}}))


def test_parse_config_vacio_no_apaga_a_nadie():
    """El lado seguro: un archivo sin apagados significa "todos visibles", nunca "nadie"."""
    cfg = parse_config(json.dumps({"apagados": {}}))
    assert config_rows(cfg) == []


def test_config_rows_devuelve_todo_en_false_y_ordenado():
    cfg = {"apagados": {"sistemas": [{"operador": "Liz", "comentario": ""}],
                        "datos": [{"operador": "Kathya", "comentario": ""}]}}
    # activo=False siempre: por eso están en el archivo. Orden fijo -> diff de git estable.
    assert config_rows(cfg) == [("datos", "Kathya", False), ("sistemas", "Liz", False)]


def test_config_from_rows_descarta_los_activos():
    """dump(BD) -> archivo: los activos NO se escriben, son el default."""
    filas = [("sistemas", "Mel", True), ("sistemas", "Liz", False), ("datos", "Alex", True)]
    cfg = config_from_rows(filas, criterio="manual")
    assert cfg["apagados"] == {"sistemas": [{"operador": "Liz", "comentario": ""}]}
    assert "datos" not in cfg["apagados"]        # no tenía apagados -> no aparece la cuenta
    assert config_rows(parse_config(json.dumps(cfg))) == [("sistemas", "Liz", False)]


# --- SQL -------------------------------------------------------------------

def test_ensure_table_es_idempotente_y_keyea_por_cuenta_y_nombre():
    cur = _FakeCursor()
    ensure_table(cur)
    sql = " ".join(q for q, _ in cur.executed)
    assert "CREATE TABLE IF NOT EXISTS operator_status" in sql
    # clave (account, operator_name): 38 de 67 operadores no están en `users`, no hay
    # user_id confiable para keyear.
    assert "PRIMARY KEY (account, operator_name)" in sql
    assert "DEFAULT true" in sql


def test_seed_from_config_NO_pisa_lo_que_ya_esta_en_la_bd():
    """Al arrancar el contenedor la semilla sólo LLENA huecos. Si pisara, cada deploy
    borraría los cambios hechos desde la UI en el server, que es justo lo que no podemos
    ver. Para que el archivo gane hay que pedirlo (apply_config)."""
    cur = _FakeCursor()
    cfg = {"apagados": {"datos": [{"operador": "Kathya", "comentario": ""}]}}
    seed_from_config(cur, cfg)
    sql, params = cur.executed[-1]
    assert "ON CONFLICT (account, operator_name) DO NOTHING" in sql
    assert params == [("datos", "Kathya", False, "seed:config")]


def test_apply_config_pisa_Y_reactiva_a_los_que_no_estan_en_el_archivo():
    """`--pisar` significa "el archivo es la verdad". Con formato de excepciones eso incluye
    PRENDER a cualquiera que esté apagado en la BD y ya no figure en el archivo: si no,
    sacar a alguien de la lista no tendría efecto y el archivo mentiría."""
    cur = _FakeCursor()
    cfg = {"apagados": {"datos": [{"operador": "Kathya", "comentario": ""}]}}
    apply_config(cur, cfg, updated_by="script:load")
    sqls = [q for q, _ in cur.executed]
    # apaga a los listados...
    assert any("DO UPDATE" in q and "activo = EXCLUDED.activo" in q for q in sqls)
    # ...y reactiva al resto DE ESA CUENTA (nunca de una cuenta ausente del archivo)
    reactiva = [(q, p) for q, p in cur.executed if "activo = true" in q]
    assert reactiva, "falta el UPDATE que reactiva a los no listados"
    q, p = reactiva[0]
    assert "account = %(account)s" in q and p["account"] == "datos"
    assert p["mantener_apagados"] == ["Kathya"]


def test_apply_config_sin_apagados_reactiva_a_todos_los_de_esa_cuenta():
    cur = _FakeCursor()
    apply_config(cur, {"apagados": {"datos": []}})
    reactiva = [(q, p) for q, p in cur.executed if "activo = true" in q]
    assert reactiva and reactiva[0][1]["mantener_apagados"] == []


def test_inactive_names_consulta_los_APAGADOS_no_los_activos():
    """Se listan los INACTIVOS a propósito: así un operador que aparece por primera vez
    (alguien que entró a trabajar hoy) no tiene fila, no está en la lista de apagados, y se
    lo considera activo. El default seguro es VISIBLE; si preguntáramos por los activos, el
    que falta quedaría oculto sin que nadie se entere."""
    cur = _FakeCursor(rows=[("MariCruz",), ("Liz",)])
    inactivos = inactive_names(cur, "sistemas")
    sql, params = cur.executed[-1]
    assert "activo = false" in sql
    assert params == {"account": "sistemas"}
    assert inactivos == {"MariCruz", "Liz"}


# --- sugerencia por actividad ----------------------------------------------

def test_suggest_from_activity_aplica_el_umbral_y_es_puro():
    """Lo que usa el bootstrap y el botón "sugerir por actividad" del modal. Con umbral 100
    y 30 días sobre los datos reales reproduce EXACTO la lista de 18 que dio el negocio."""
    rows = [
        ("sistemas", "Joseph", 7170, 2053),
        ("sistemas", "Alex", 1827, 100),      # justo en el borde -> activo
        ("sistemas", "Santiago", 270, 77),    # debajo -> apagado
        ("sistemas", "MariCruz", 3336, 18),   # histórico: mucho volumen, nada reciente
        ("datos", "Mogost", 1580, 710),
    ]
    assert suggest_from_activity(rows, umbral=100) == [
        ("datos", "Mogost", True),
        ("sistemas", "Alex", True),
        ("sistemas", "Joseph", True),
        ("sistemas", "MariCruz", False),
        ("sistemas", "Santiago", False),
    ]


def test_suggest_from_activity_no_mira_el_volumen_historico():
    """MariCruz tiene 3.336 sesiones totales y 18 recientes: el volumen histórico NO puede
    mantenerla activa, porque ese volumen es justamente el que ensucia los cuadros."""
    out = dict(((c, o), a) for c, o, a in
               suggest_from_activity([("sistemas", "MariCruz", 999999, 18)], umbral=100))
    assert out[("sistemas", "MariCruz")] is False
