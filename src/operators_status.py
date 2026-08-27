"""Estado ACTIVO/INACTIVO de los operadores: baja lógica, archivo <-> BD.

PARA QUE SIRVE. Los cuadros por operador se llenan de gente que ya no trabaja: en
`sistemas` hay 50 operadores y solo 19 con actividad real, y los 31 restantes aportan
27.398 sesiones históricas que ensucian promedios y rankings. Apagar un operador lo
esconde de los cuadros SIN borrar nada: es una baja lógica.

DONDE VIVE EL DATO Y POR QUE. La FUENTE en runtime es la tabla `operator_status` de esta
misma base. No un archivo en el contenedor: al server solo se le puede hacer `git pull`,
no leer archivos, así que un cambio hecho desde la UI en producción sería invisible. La
base, en cambio, se copia entera y baja con el resto de los datos.

FORMATO DEL ARCHIVO: lista SOLO a los APAGADOS. "Activo" es el default de la BD, así que
no es una decisión que haya que registrar — es la ausencia de una. Enumerar además a los
activos era redundante y abría la puerta a que las dos listas se contradigan. Como efecto
lateral bueno, el archivo se lee como lo que es: la lista de excepciones.

El ARCHIVO (`config/operadores.json`) es el espejo auditable y la semilla:
  - `scripts/dump_operadores.py`  BD -> archivo. Se corre sobre la copia de prod; el diff
    de git muestra qué cambiaron en el server.
  - `scripts/load_operadores.py`  archivo -> BD (pisa; explícito).
  - `seed_from_config()`          archivo -> BD al arrancar el contenedor, SOLO llenando
    huecos (ON CONFLICT DO NOTHING). Si pisara, cada deploy borraría los cambios hechos
    desde la UI, que es exactamente lo que no podemos ver.

CLAVE = (account, operator_name). No `user_id`: 38 de los 67 operadores que mandan
mensajes en `sistemas` no existen en la tabla `users` y se identifican por la firma
`*Nombre:*` del cuerpo. El nombre usado es el RESUELTO, el mismo con el que agrupan los
cuadros: `identidad.OPERADOR_RESUELTO`, que es la FUENTE UNICA de esa regla (este modulo
tenia su propia copia de la expresion Y del translate de acentos, con el bug de la ñ que ya
se habia arreglado en queries.py -- por eso seguia apareciendo la etiqueta en el modal).

DEFAULT = ACTIVO. Un operador sin fila se considera activo, y por eso se consultan los
INACTIVOS (ver `inactive_names`). Alguien que entra a trabajar hoy tiene que aparecer solo;
si el default fuera "oculto", nadie se enteraría de que falta.
"""
from __future__ import annotations

import json

from src.identidad import HAY_OPERADOR, OPERADOR_RESUELTO, clave_sql
from pathlib import Path

CONFIG_VERSION = 2   # v2: el archivo lista solo apagados (v1 enumeraba todos)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "operadores.json"

_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS operator_status (
        account       text        NOT NULL,
        operator_name text        NOT NULL,
        activo        boolean     NOT NULL DEFAULT true,
        updated_at    timestamptz NOT NULL DEFAULT now(),
        updated_by    text,
        PRIMARY KEY (account, operator_name)
    )""",
    # Índice para el filtro de los cuadros: se piden los inactivos de UNA cuenta.
    "CREATE INDEX IF NOT EXISTS idx_operator_status_off "
    "ON operator_status (account) WHERE activo = false",
)

# LA MISMA CLAVE QUE LA LECTURA. La PK es `(account, operator_name)` — el string EXACTO —
# pero los cuatro puntos de lectura comparan por CLAVE (minusculas, sin tildes) desde el
# 2026-08-07. Escribir por la PK y leer por la clave no falla: MIENTE.
#
# Medido en produccion el 2026-08-27: la tabla tenia 'RAMIREZ' (sembrado el 06-ago del dump
# del 04-ago) y el modal mandaba 'Ramirez' (el nombre RESUELTO, que sale de users.name).
# `ON CONFLICT` no matcheaba, insertaba una SEGUNDA fila, y como `_ADMIN_ROWS` hace
# `coalesce(bool_and(os.activo), true)` sobre el join por clave, matcheaba las dos:
# `false AND true = false`. El PUT devolvia 200 {"actualizados": 1} y el operador NO se
# prendia NUNCA. Ramirez tenia 222 sesiones recientes y actividad ese mismo dia.
#
# Se compara por clave en vez de renombrar las filas, por el mismo motivo que en la lectura:
# renombrar arregla el caso de hoy y deja la trampa armada para la proxima grafia.
_CLAVE_FILA = clave_sql("operator_name")
_CLAVE_ARG = clave_sql("%(operador)s")

# Siembra del arranque: NO PISA. Antes se apoyaba en `ON CONFLICT DO NOTHING`, que mira la
# PK exacta — asi que con una grafia distinta re-insertaba la fila apagada en CADA arranque
# del contenedor y deshacia el cambio hecho desde la UI en el siguiente deploy.
_SEED = f"""
INSERT INTO operator_status (account, operator_name, activo, updated_by)
SELECT %(account)s, %(operador)s, %(activo)s, %(updated_by)s
 WHERE NOT EXISTS (
   SELECT 1 FROM operator_status
    WHERE account = %(account)s AND {_CLAVE_FILA} = {_CLAVE_ARG}
 )
"""

# Escritura del modal y de `--pisar`. ACTUALIZA todo lo que matchee por clave (en plural: si
# ya quedaron dos filas duplicadas de antes, las dos tienen que terminar iguales, porque el
# `bool_and` de la lectura se apaga con que UNA siga en false) e INSERTA solo si no habia
# nada. Sin `LIMIT`: alcanzar una sola fila dejaria a la otra apagando.
_APPLY = f"""
WITH tocadas AS (
  UPDATE operator_status
     SET activo = %(activo)s, updated_by = %(updated_by)s, updated_at = now()
   WHERE account = %(account)s AND {_CLAVE_FILA} = {_CLAVE_ARG}
  RETURNING 1
)
INSERT INTO operator_status (account, operator_name, activo, updated_by)
SELECT %(account)s, %(operador)s, %(activo)s, %(updated_by)s
 WHERE NOT EXISTS (SELECT 1 FROM tocadas)
"""

# Segunda mitad de `--pisar`: prende a los que ya no figuran en el archivo. Con lista vacía
# el NOT EXISTS es verdadero para toda fila, así que un archivo sin apagados reactiva a
# todos los de esa cuenta — que es exactamente lo que pidió, y la misma semántica que tenía
# el `= ANY(...)` de antes.
#
# Compara por CLAVE, como las otras dos escrituras: con string exacto, una grafía distinta
# saca al operador de la lista de "mantener apagado" y lo PRENDE sin que nadie lo pida. Es
# el error simétrico del de `_APPLY` y es igual de silencioso.
_REACTIVAR = f"""
UPDATE operator_status
   SET activo = true, updated_by = %(updated_by)s, updated_at = now()
 WHERE account = %(account)s AND activo = false
   AND NOT EXISTS (
         SELECT 1 FROM unnest(%(mantener_apagados)s::text[]) AS m
          WHERE {clave_sql("m")} = {_CLAVE_FILA}
       )
"""

# Se consultan los APAGADOS, no los activos: ver el docstring del módulo (default = activo).
_INACTIVE = """
SELECT operator_name FROM operator_status
 WHERE account = %(account)s AND activo = false
"""

_DUMP = """
SELECT account, operator_name, activo FROM operator_status
 ORDER BY account, operator_name
"""


def ensure_table(cur) -> None:
    """Crea la tabla y su índice si faltan (idempotente, patrón self-healing del proyecto)."""
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)


# --- archivo ----------------------------------------------------------------

_NOTA = ("Solo se listan los operadores APAGADOS. Cualquiera que no esté acá está ACTIVO: "
         "es el default, para que alguien que entra a trabajar hoy aparezca solo. "
         "Generado por scripts/dump_operadores.py; editable a mano.")


def parse_config(text: str) -> dict:
    """Valida y normaliza el JSON. Falla fuerte y temprano: este archivo decide a quién se
    ve y a quién no, así que un typo silencioso no es aceptable.

    Acepta dos formas por entrada, para que editarlo a mano sea barato:
        "MariCruz"                                   (solo el nombre)
        {"operador": "MariCruz", "comentario": "..."} (con el por qué)
    """
    raw = json.loads(text)
    apagados = raw.get("apagados")
    if not isinstance(apagados, dict):
        raise ValueError("config de operadores: falta el objeto 'apagados'")
    out: dict = {"version": raw.get("version", CONFIG_VERSION),
                 "criterio": raw.get("criterio", ""),
                 "generado_en": raw.get("generado_en", ""),
                 "nota": raw.get("nota", _NOTA),
                 "apagados": {}}
    for cuenta, ops in apagados.items():
        if not isinstance(ops, list):
            raise ValueError(f"config de operadores: 'apagados.{cuenta}' debe ser una lista")
        vistos: set[str] = set()
        norm = []
        for i, op in enumerate(ops):
            entrada = {"operador": op} if isinstance(op, str) else op
            if not isinstance(entrada, dict):
                raise ValueError(f"config de operadores: 'apagados.{cuenta}[{i}]' invalido")
            nombre = str(entrada.get("operador", "")).strip()
            if not nombre:
                raise ValueError(
                    f"config de operadores: 'apagados.{cuenta}[{i}]' sin 'operador'")
            if nombre in vistos:
                raise ValueError(f"config de operadores: '{nombre}' duplicado en '{cuenta}'")
            vistos.add(nombre)
            norm.append({"operador": nombre,
                         "comentario": str(entrada.get("comentario", "")).strip()})
        out["apagados"][cuenta] = norm
    return out


def load_config(path: Path | None = None) -> dict:
    """Lee y valida el archivo. Si no existe, devuelve una config vacía — no explota: un
    entorno nuevo sin archivo simplemente no tiene a nadie apagado (lado seguro)."""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {"version": CONFIG_VERSION, "criterio": "", "generado_en": "",
                "nota": _NOTA, "apagados": {}}
    return parse_config(p.read_text())


def config_rows(cfg: dict) -> list[tuple]:
    """[(cuenta, operador, False)] ordenado. PURO. Todos en False: por eso están en el
    archivo. El orden fijo mantiene estable el archivo generado, así el diff de git muestra
    cambios reales y no reordenamientos."""
    return sorted(
        (cuenta, op["operador"], False)
        for cuenta, ops in cfg.get("apagados", {}).items()
        for op in ops
    )


def config_accounts(cfg: dict) -> list[str]:
    """Cuentas mencionadas en el archivo. `apply_config` sólo puede reactivar dentro de
    estas: una cuenta ausente del archivo no se toca."""
    return sorted(cfg.get("apagados", {}))


def config_from_rows(rows, criterio: str = "", generado_en: str = "") -> dict:
    """[(cuenta, operador, activo)] -> forma del archivo. PURO (la fecha entra por parámetro
    para que sea determinista y testeable). Los ACTIVOS se descartan: son el default."""
    apagados: dict = {}
    for cuenta, operador, activo in sorted(rows):
        if activo:
            continue
        apagados.setdefault(cuenta, []).append({"operador": operador, "comentario": ""})
    return {"version": CONFIG_VERSION, "criterio": criterio,
            "generado_en": generado_en, "nota": _NOTA, "apagados": apagados}


def dump_config(cur, criterio: str = "", generado_en: str = "") -> dict:
    """Lee la tabla y devuelve la config con la forma del archivo (BD -> archivo)."""
    cur.execute(_DUMP)
    return config_from_rows(cur.fetchall(), criterio=criterio, generado_en=generado_en)


# --- BD ---------------------------------------------------------------------

def _params(filas, updated_by: str) -> list[dict]:
    """(cuenta, operador, activo) -> los parámetros con nombre que esperan las escrituras.

    Con nombre y no posicionales: las tres consultas repiten `account` y `operador` en dos
    lugares (el match por clave y el INSERT), y con `%s` posicional eso se convierte en
    contar placeholders a mano — que es exactamente el tipo de error que este módulo ya pagó.
    """
    return [{"account": c, "operador": o, "activo": a, "updated_by": updated_by}
            for c, o, a in filas]


def seed_from_config(cur, cfg: dict, updated_by: str = "seed:config") -> int:
    """Siembra SIN pisar. Es lo que corre al arrancar el contenedor: llena huecos y respeta
    cualquier cambio hecho desde la UI — incluido el hecho sobre otra grafía del nombre."""
    filas = _params(config_rows(cfg), updated_by)
    if filas:
        cur.executemany(_SEED, filas)
    return len(filas)


def apply_config(cur, cfg: dict, updated_by: str = "script:load") -> int:
    """Aplica el archivo PISANDO la BD. Explícito y a mano: restaurar un estado conocido o
    empujar un cambio revisado en git.

    Con formato de excepciones, "el archivo es la verdad" tiene DOS mitades:
      1. apagar a los listados, y
      2. REACTIVAR a cualquiera que esté apagado en la BD y ya no figure en el archivo.
    Sin la segunda, sacar a alguien de la lista no tendría efecto y el archivo mentiría.
    Solo se tocan las cuentas que el archivo menciona: una cuenta ausente se deja intacta.
    """
    filas = _params(config_rows(cfg), updated_by)
    if filas:
        cur.executemany(_APPLY, filas)
    for cuenta in config_accounts(cfg):
        mantener = [o for c, o, _ in config_rows(cfg) if c == cuenta]
        cur.execute(_REACTIVAR, {"account": cuenta, "mantener_apagados": mantener,
                                 "updated_by": updated_by})
    return len(filas)


def inactive_names(cur, account: str) -> set[str]:
    """Nombres APAGADOS de una cuenta. Se pregunta por los apagados (y no por los activos)
    para que un operador sin fila quede ACTIVO por default; ver docstring del módulo."""
    cur.execute(_INACTIVE, {"account": account})
    return {r[0] for r in cur.fetchall()}



# Actividad de TODOS los operadores de las DOS cuentas. Espeja `_ADMIN_ROWS` (mismo nombre
# resuelto, misma ventana, mismo criterio de `recientes`) pero sin scopear por cuenta y sin
# el estado guardado: lo consume el bootstrap del archivo, que necesita las dos.
# ESTABA SIN DEFINIR: `activity_rows` lo usaba y tiraba NameError, o sea que
# scripts/dump_operadores.py -- el que genera config/operadores.json -- estaba roto, y no lo
# atrapaba nada porque ningun test llamaba a la funcion.
_ACTIVITY = f"""
WITH ancla AS (
  SELECT account, max(conversation_created_at) AS ultimo
    FROM conversation_scores GROUP BY account
)
SELECT cs.account,
       {OPERADOR_RESUELTO} AS operador,
       count(*) AS sesiones,
       count(*) FILTER (
         WHERE cs.conversation_created_at >= a.ultimo - make_interval(days => %(dias)s)
           AND cs.eval_status = 'evaluated'
       ) AS recientes,
       max(cs.conversation_created_at)::date AS ultima_actividad
  FROM conversation_scores cs
  JOIN ancla a ON a.account = cs.account
  LEFT JOIN users u ON u.id = cs.user_id AND u.account = cs.account
 GROUP BY 1, 2
 ORDER BY 1, 4 DESC, 3 DESC
"""


def activity_rows(cur, dias: int = 30) -> list[tuple]:
    """[(cuenta, operador, sesiones, recientes, ultima_actividad)] para todos los
    operadores de las dos cuentas. Lo consume el bootstrap del archivo y el modal."""
    cur.execute(_ACTIVITY, {"dias": dias})
    return cur.fetchall()


# Lo que consume el modal: actividad + estado guardado, de UNA cuenta. El LEFT JOIN con
# coalesce(activo, true) materializa el default seguro: un operador sin fila sale ACTIVO.
_ADMIN_ROWS = f"""
WITH ancla AS (
  SELECT max(conversation_created_at) AS ultimo
    FROM conversation_scores WHERE account = %(account)s
)
SELECT {OPERADOR_RESUELTO} AS operador,
       count(*) AS sesiones,
       -- `recientes` cuenta SOLO las EVALUADAS. Es el numero contra el que el modal pre-marca
       -- activos, y de eso depende quien aparece en los cuadros -- que muestran evaluadas.
       -- Contando tambien las `skipped` (casi una decima parte de la tabla) el numero prometia
       -- un aporte que no existe: medido el 2026-08-11 sobre la copia, de 29 operadores con
       -- actividad 24 cruzaban el umbral de 100 con el numero viejo y 22 con este, o sea que 2
       -- entraban SOLO por sesiones que nunca se calificaron.
       -- `sesiones` queda SIN filtrar a proposito: es el volumen historico de la persona, no
       -- una medida de aporte, y la columna del modal se llama asi.
       -- OJO: nada de signos de porcentaje sueltos en estos comentarios -- psycopg los lee
       -- como placeholder y revienta con "incomplete placeholder". Ver el guard en los tests.
       count(*) FILTER (
         WHERE cs.conversation_created_at >= a.ultimo - make_interval(days => %(dias)s)
           AND cs.eval_status = 'evaluated'
       ) AS recientes,
       max(cs.conversation_created_at)::date AS ultima_actividad,
       coalesce(bool_and(os.activo), true) AS activo
  FROM conversation_scores cs
  CROSS JOIN ancla a
  LEFT JOIN users u ON u.id = cs.user_id AND u.account = cs.account
  LEFT JOIN operator_status os
         ON os.account = cs.account
        AND {clave_sql("os.operator_name")} = {clave_sql(OPERADOR_RESUELTO)}
 WHERE cs.account = %(account)s
   AND {HAY_OPERADOR}
 GROUP BY 1
 ORDER BY 3 DESC, 2 DESC
"""


def admin_rows(cur, account: str, dias: int = 30) -> list[dict]:
    """Operadores de una cuenta con su actividad y su estado guardado, para el modal."""
    cur.execute(_ADMIN_ROWS, {"account": account, "dias": dias})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def set_many(cur, account: str, pares, updated_by: str = "ui") -> int:
    """Aplica una tanda de (operador, activo) de UNA cuenta. Lo usa el PUT del modal: se
    guarda todo junto, no de a uno, para que no quede a medias si se corta."""
    filas = _params(((account, nombre, bool(activo)) for nombre, activo in pares), updated_by)
    if filas:
        cur.executemany(_APPLY, filas)
    return len(filas)


def suggest_from_activity(rows, umbral: int = 100) -> list[tuple]:
    """[(cuenta, operador, activo)] ordenado, con activo = recientes >= umbral. PURO.

    Mira SOLO la actividad reciente, nunca el volumen histórico: ese volumen es
    precisamente lo que ensucia los cuadros (MariCruz tiene 3.336 sesiones y 18 recientes).
    Con umbral=100 y 30 días sobre los datos reales, esto reproduce exacto la lista de 18
    operadores que dio el negocio — así se validó el umbral.
    """
    return sorted((r[0], r[1], int(r[3]) >= umbral) for r in rows)
