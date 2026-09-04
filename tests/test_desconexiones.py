"""El historial de conexion/desconexion de operadores: la CAPTURA, no la alerta.

POR QUE ES UN TRIGGER Y NO UN POLLER. `users` es un SNAPSHOT: el ETL pisa `status` y
`refreshed_at` en cada sync y el estado anterior se pierde. Un poller cada 60 s pierde
toda transicion que ocurra entre dos pasadas; el trigger ve TODOS los cambios por
construccion, y ademas es agnostico del escritor (el ETL, un UPDATE a mano, o el ETL
reescrito el año que viene).

LO QUE ESTOS TESTS PROTEGEN son las tres decisiones que, si se caen, no dan error: dan un
historial silenciosamente inutil o un ETL roto. Se prueba el TEXTO del DDL porque en este
repo no hay tests contra base real; la verificacion de que el trigger DISPARA se hizo a
mano contra `whaticket_copia` y esta anotada en docs/auditoria-desconexiones.md.
"""
from __future__ import annotations

from src import desconexiones


class _FakeCursor:
    """Registra el SQL que le mandan, normalizado a una linea.

    `dueno_coincide`: la respuesta al chequeo de dueños. `True` / `False` / `None` ("no se
    pudo determinar"), o el centinela `SIN_FILA` para que `fetchone` devuelva `None`.
    `levantar_en`: subcadena del SQL con la que la sentencia debe reventar.
    """

    SIN_FILA = object()

    def __init__(self, dueno_coincide=True, levantar_en=None):
        self.sql: list[str] = []
        self._dueno = dueno_coincide
        self._levantar_en = levantar_en

    def execute(self, sql, params=None):
        texto = " ".join(str(sql).split())
        self.sql.append(texto)
        if self._levantar_en and self._levantar_en in texto:
            raise RuntimeError(
                "more than one row returned by a subquery used as an expression")

    def fetchone(self):
        if self._dueno is _FakeCursor.SIN_FILA:
            return None
        return (self._dueno,)


def _ddl() -> str:
    cur = _FakeCursor()
    desconexiones.ensure_table(cur)
    return " ".join(cur.sql)


# --- la idempotencia --------------------------------------------------------

def test_cada_sentencia_del_DDL_se_puede_VOLVER_A_CORRER():
    """`ensure_table` corre en CADA ciclo del worker (cada 60 s). Una sentencia que no
    tolere volver a correr tumba el ciclo entero, y con el las alertas.

    LA INVARIANTE ES SEMANTICA, NO UN STRING. La version anterior de este test exigia
    `IF NOT EXISTS` o `OR REPLACE` literales, y ese proxy fue lo que me hizo RECHAZAR el
    `REVOKE EXECUTE FROM PUBLIC` --que es re-ejecutable por definicion en SQL-- con el
    argumento de que "rompia la invariante de idempotencia". El proxy bloqueo un arreglo de
    seguridad correcto. `REVOKE` entra en la lista porque revocar algo ya revocado es un
    no-op, no un error."""
    cur = _FakeCursor()
    desconexiones.ensure_table(cur)
    reejecutable = ("IF NOT EXISTS", "OR REPLACE", "REVOKE")
    for stmt in cur.sql:
        assert any(f in stmt for f in reejecutable), \
            f"sentencia que no se puede volver a correr: {stmt[:120]}"


def test_el_trigger_se_reemplaza_SIN_dejar_una_ventana_sin_trigger():
    """`DROP` + `CREATE` deja microsegundos en los que un UPDATE del ETL no se registra.
    PG 16 tiene `CREATE OR REPLACE TRIGGER`: se usa ese."""
    ddl = _ddl()
    assert "CREATE OR REPLACE TRIGGER" in ddl
    assert "DROP TRIGGER" not in ddl


# --- las tres decisiones que si se caen NO dan error ------------------------

def test_el_trigger_solo_registra_cambios_REALES_de_status():
    """SIN ESTE `WHEN` el historial nace basura. El upsert del ETL toca las 49 filas en
    cada sync y `AFTER UPDATE OF status` dispara aunque el valor sea el mismo: serian 49
    filas de ruido por corrida en lugar de solo las transiciones."""
    ddl = _ddl()
    assert "AFTER UPDATE OF status ON users" in ddl
    assert "WHEN (OLD.status IS DISTINCT FROM NEW.status)" in ddl


def test_el_trigger_NO_puede_abortar_la_escritura_del_ETL():
    """MISMA REGLA QUE `errores.registrar` Y `worker._registrar_fallo`: un registrador
    roto no puede tumbar a quien lo llama. Aca es mas grave que en Python, porque el
    trigger corre DENTRO de la transaccion del ETL: si la tabla de historial no existe o
    no acepta la fila, el UPDATE de `users` falla y el ETL deja de ingestar."""
    ddl = _ddl()
    assert "EXCEPTION" in ddl and "WHEN OTHERS" in ddl


def test_el_historial_guarda_LOS_DOS_relojes():
    """`last_seen` es cuando lo vio WHATICKET; `detected_at` es cuando lo vimos NOSOTROS.
    Su DIFERENCIA es la latencia del sync del ETL, que hoy no se puede medir de ninguna
    otra forma (`users` se pisa entero y `sync_state` no tiene clave para lookups).
    Guardar uno solo hace la pregunta imposible para siempre."""
    ddl = _ddl()
    assert "last_seen" in ddl
    assert "detected_at" in ddl


def test_se_guarda_el_estado_ANTERIOR_y_no_solo_el_nuevo():
    """Sin `status_ant` una fila no dice si fue una desconexion o una reconexion."""
    ddl = _ddl()
    assert "status_ant" in ddl
    assert "status_nuevo" in ddl


# --- las trampas conocidas de este repo -------------------------------------

def test_el_DDL_no_lleva_NINGUN_porcentaje():
    """psycopg parsea el SQL completo buscando placeholders, comentarios incluidos (ver
    tests/test_queries.py). Un `%` suelto en el cuerpo de la funcion plpgsql revienta con
    IndexError en cuanto alguien le pase params. Se evita entero."""
    assert "%" not in _ddl()


def test_la_tabla_NO_se_llama_como_el_toggle_de_operadores():
    """`operator_status.activo` es la baja LOGICA del tablero (prender/apagar un operador);
    `users.status` es la CONEXION (online/offline). Son cosas distintas y un nombre como
    `operator_status_history` las confunde para siempre."""
    ddl = _ddl()
    assert "operator_status_history" not in ddl
    assert "conexiones_operador" in ddl


# --- el INSERT corre con los privilegios de QUIEN DISPARA -------------------

def test_la_funcion_es_SECURITY_DEFINER():
    """EL BUG QUE ESTO IMPIDE, reproducido el 2026-09-03 contra `whaticket_copia`.

    Por defecto `CREATE FUNCTION` es SECURITY INVOKER: el cuerpo corre con los privilegios
    de QUIEN dispara el trigger, o sea el rol del ETL, no el dueño de la funcion. Si ese
    rol no tiene INSERT en `conexiones_operador` (ni USAGE en su secuencia, que arranca sin
    ACL), el INSERT da permission denied, lo atrapa el `EXCEPTION WHEN OTHERS` y el
    historial queda VACIO PARA SIEMPRE sin un solo error visible.

    Medido con un rol que solo tenia SELECT+UPDATE sobre `users`:
        filas capturadas: 0
        contar pg_trigger devuelve: 1   <- todo verde, y miente

    El savepoint de `asegurar_sin_romper` cubre el `CREATE TRIGGER`, que es en tiempo de
    DEPLOY. Esto es en tiempo de EJECUCION, y es el que no se ve.
    """
    assert "SECURITY DEFINER" in _ddl()


def test_la_funcion_fija_el_search_path():
    """SECURITY DEFINER SIN `search_path` FIJO ES UN AGUJERO DE ESCALADA: quien pueda crear
    un objeto en un schema que venga antes en el path del invocador secuestra la resolucion
    de nombres y corre codigo con los privilegios del dueño. `pg_temp` va ULTIMO para que
    una tabla temporaria no pueda sombrear a `conexiones_operador`."""
    ddl = _ddl()
    assert "SET search_path" in ddl
    assert "pg_temp" in ddl


def test_se_revoca_EXECUTE_a_PUBLIC():
    """`EXECUTE` es PUBLIC por defecto, y con SECURITY DEFINER eso alcanza para ENVENENAR
    el log de auditoria. No es ejecucion arbitraria: una funcion `RETURNS trigger` no se
    puede invocar directo. Pero cualquier rol que sea dueño de una tabla con columnas
    compatibles puede colgar ESTA funcion como trigger de SU tabla y escribir filas con los
    privilegios del dueño.

    REPRODUCIDO el 2026-09-03 con un rol no-superusuario:
        colgo la funcion DEFINER de SU tabla: SI
        fila envenenada: ('FALSA', 'Operador Inventado', 'online', 'offline')

    El comentario anterior del modulo decia "no hay camino de escalada". Era falso.
    """
    assert "REVOKE EXECUTE ON FUNCTION" in _ddl()
    assert "FROM PUBLIC" in _ddl()


def test_sin_transaccion_abierta_NO_levanta(monkeypatch):
    """`SAVEPOINT` solo existe dentro de un bloque de transaccion: con `autocommit=True`
    pedirlo levanta NoActiveSqlTransaction. Y esta funcion, cuyo contrato es NO poder romper
    al llamador, rompia al llamador.

    Hoy el unico llamador es `alertas.barrer`, que usa `conn.commit()` --o sea sin
    autocommit-- asi que no era un bug en produccion. Pero era una mina: cualquier llamador
    futuro con autocommit caia en el `except` ancho de `barrer` y las alertas quedaban en
    ceros. Lo destapo el propio script de verificacion.

    Con autocommit no hay nada que proteger: cada sentencia es su propia transaccion, asi
    que un DDL fallido no arrastra al resto."""
    class _SinTransaccion(_FakeCursor):
        def execute(self, sql, params=None):
            if "SAVEPOINT" in str(sql):
                raise RuntimeError("SAVEPOINT can only be used in transaction blocks")
            super().execute(sql, params)

    cur = _SinTransaccion()
    assert desconexiones.asegurar_sin_romper(cur, log=lambda m: None) is True
    assert any("CREATE TABLE IF NOT EXISTS conexiones_operador" in s for s in cur.sql), \
        "sin savepoint el DDL tiene que correr igual"


# --- la invariante de dueños ------------------------------------------------

def test_se_verifica_que_funcion_y_tabla_tengan_el_MISMO_dueno():
    """SECURITY DEFINER solo alcanza si el dueño de la funcion puede escribir la tabla.

    EL CASO QUE SE ESCAPA: `CREATE TABLE IF NOT EXISTS` es un NO-OP si la tabla ya existe,
    sin importar QUIEN la creo. Si la tabla la creo otro rol, la funcion corre como su
    propio dueño y el INSERT vuelve a dar permission denied -- y `prosecdef` seguiria
    diciendo `true`, asi que el chequeo de SECURITY DEFINER no lo detecta."""
    cur = _FakeCursor()
    desconexiones.asegurar_sin_romper(cur)
    junto = " ".join(cur.sql)
    assert "proowner" in junto and "relowner" in junto, \
        "no se verifica que funcion y tabla compartan dueño"


def test_dueno_distinto_se_reporta_como_captura_ROTA(monkeypatch):
    """Con dueños distintos la captura NO funciona, aunque el DDL haya corrido sin error.
    Tiene que devolver False y dejarlo en `errors`, no en verde."""
    from src import errores

    visto = {}
    monkeypatch.setattr(errores, "registrar",
                        lambda c, e=None, **k: visto.update(component=c, context=k.get("context")))
    cur = _FakeCursor(dueno_coincide=False)
    assert desconexiones.asegurar_sin_romper(cur, log=lambda m: None) is False
    assert visto.get("component") == "arranque"


def test_si_la_consulta_de_duenos_LEVANTA_no_se_rompe_el_llamador(monkeypatch):
    """MODO 6. La guarda que cerro el modo 5 abrio este: el chequeo de dueños quedo
    DESPUES del `RELEASE SAVEPOINT` y sin `try`. Si esa sentencia levanta, la excepcion
    sube a `alertas.ensure_table` -> `alertas.barrer`, cuyo unico `try` devuelve todo en
    ceros: las alertas VIP se apagan en silencio. Es exactamente el daño que el savepoint
    existe para evitar, y el contrato que el modulo declara.

    LA VIA CONCRETA, reproducida el 2026-09-03: con un homonimo en otro schema, la version
    anterior de `_DUENOS_SQL` (subconsultas por `proname`/`relname` sin calificar) daba
        CardinalityViolation: more than one row returned by a subquery used as an expression
    y ademas dejaba la transaccion en `InFailedSqlTransaction` -- asi que un `try/except` en
    Python tampoco alcanzaba. Cada operacion necesita su PROPIO savepoint."""
    from src import errores

    monkeypatch.setattr(errores, "registrar", lambda *a, **k: None)
    cur = _FakeCursor(levantar_en="pg_trigger")
    assert desconexiones.asegurar_sin_romper(cur, log=lambda m: None) is False
    assert any("ROLLBACK TO SAVEPOINT" in s for s in cur.sql), \
        "el chequeo de dueños necesita su propio savepoint, no solo un try"


def test_NULL_no_es_verde(monkeypatch):
    """`no se` NO puede leerse como `bien` en un modulo cuyo punto entero es que el verde
    signifique algo. Si un objeto no existe la expresion da NULL, y el codigo anterior
    hacia `fila[0] is False` -> `None is False` es False -> caia al `return True`.
    Reproducido: la consulta devolvia `(None,)` y el modulo reportaba verde."""
    from src import errores

    monkeypatch.setattr(errores, "registrar", lambda *a, **k: None)
    assert desconexiones.asegurar_sin_romper(
        _FakeCursor(dueno_coincide=None), log=lambda m: None) is False


def test_sin_trigger_tampoco_es_verde(monkeypatch):
    """Si el trigger no existe la consulta no devuelve NINGUNA fila. `fetchone()` da None y
    el codigo anterior habria explotado con TypeError al indexar."""
    from src import errores

    monkeypatch.setattr(errores, "registrar", lambda *a, **k: None)
    assert desconexiones.asegurar_sin_romper(
        _FakeCursor(dueno_coincide=_FakeCursor.SIN_FILA), log=lambda m: None) is False


def test_el_chequeo_de_duenos_NO_puede_dar_multifila():
    """Se pregunta por OID, no por nombre. `to_regclass` devuelve NULL en vez de error y no
    puede dar multifila; `pg_trigger` se acota por `(tgrelid, tgname)`, que es unico. Y el
    oid de la funcion sale de `tgfoid`: la que el trigger VA A LLAMAR de verdad, no la que
    resuelva el search_path del momento."""
    sql = desconexiones._DUENOS_SQL
    assert "to_regclass" in sql
    assert "tgfoid" in sql and "tgrelid" in sql
    assert "WHERE proname" not in sql and "WHERE relname" not in sql, \
        "buscar por nombre sin calificar es la via a CardinalityViolation"


def test_dueno_coincidente_no_reporta_nada(monkeypatch):
    """El camino feliz no puede ensuciar `errors`: se llenaria una fila cada 60 s."""
    from src import errores

    llamadas = []
    monkeypatch.setattr(errores, "registrar", lambda *a, **k: llamadas.append(a))
    assert desconexiones.asegurar_sin_romper(_FakeCursor(dueno_coincide=True)) is True
    assert not llamadas


# --- la captura no puede apagar las alertas que YA funcionan -----------------

def test_la_captura_va_en_un_SAVEPOINT_propio():
    """UN DDL QUE FALLA ABORTA LA TRANSACCION ENTERA en Postgres, asi que un try/except
    pelado no alcanza: la sentencia siguiente de `barrer` moriria con
    InFailedSqlTransaction y se llevaria el barrido igual. Misma trampa que documenta
    `worker.score_batch` con su `conn.rollback()`."""
    cur = _FakeCursor()
    desconexiones.asegurar_sin_romper(cur)
    junto = " ".join(cur.sql)
    assert "SAVEPOINT" in junto
    assert "RELEASE SAVEPOINT" in junto


def test_un_fallo_de_la_captura_NO_apaga_las_alertas(monkeypatch):
    """EL RIESGO REAL DE PRODUCCION. `CREATE TRIGGER` sobre `users` pide el privilegio
    TRIGGER, y `users` es del ETL: si el dashboard conecta con un rol que no lo tiene,
    esto revienta con permission denied. En la copia no se ve porque ahi el rol es
    superusuario y dueño de la tabla.

    `alertas.barrer` tiene UN solo try que arranca en `ensure_table` y un except que
    devuelve todo en ceros: sin esta guarda, una tabla de auditoria nueva apagaria en
    silencio las alertas VIP que hoy andan."""
    def revienta(cur):
        raise RuntimeError("permission denied for table users")

    monkeypatch.setattr(desconexiones, "ensure_table", revienta)
    cur = _FakeCursor()
    ok = desconexiones.asegurar_sin_romper(cur, log=lambda m: None)
    assert ok is False, "tiene que informar que NO quedo aplicada"
    assert any("ROLLBACK TO SAVEPOINT" in s for s in cur.sql), \
        "sin el rollback al savepoint la transaccion queda abortada"


def test_el_fallo_de_la_captura_DICE_algo(monkeypatch):
    """Degradar en silencio es el modo de falla que ya costo caro dos veces en este repo
    (la alerta de espera, el PUT de `operator_status`). UNA linea que distinga
    'la captura entro' de 'la captura no entro'. Es la leccion de `errores.estado`."""
    def revienta(cur):
        raise RuntimeError("permission denied for table users")

    monkeypatch.setattr(desconexiones, "ensure_table", revienta)
    dicho: list[str] = []
    desconexiones.asegurar_sin_romper(_FakeCursor(), log=dicho.append)
    assert dicho, "un fallo de la captura no puede ser silencioso"
    assert "permission denied" in " ".join(dicho)


def test_el_fallo_va_a_la_tabla_ERRORS_y_no_solo_a_stdout(monkeypatch):
    """`stdout` de un contenedor se pierde en el redeploy; `errors` sobrevive. Es el mismo
    argumento que ya esta escrito en el loop de alertas de `worker.py`:

        "A LA TABLA `errors`, NO SOLO A STDOUT. (...) si se rompe en silencio el canal se
         queda mudo y nadie distingue 'no hubo esperas largas' de 'el barrido esta muerto
         hace tres dias'."

    Componente `arranque`, que es el del vocabulario acordado para migraciones y config
    (`errores.COMPONENTES`), y sin `account` porque el DDL no es de una cuenta puntual."""
    from src import errores

    def revienta(cur):
        raise RuntimeError("permission denied for table users")

    visto = {}

    def falso_registrar(component, exc=None, *, account=None, message=None, context=None):
        visto.update(component=component, exc=exc, account=account, context=context)
        return True

    monkeypatch.setattr(desconexiones, "ensure_table", revienta)
    monkeypatch.setattr(errores, "registrar", falso_registrar)
    desconexiones.asegurar_sin_romper(_FakeCursor(), log=lambda m: None)

    assert visto.get("component") == "arranque", \
        f"componente fuera del vocabulario acordado: {visto.get('component')!r}"
    assert visto["component"] in errores.COMPONENTES
    assert visto.get("account") is None, "el DDL no es de una cuenta puntual"
    assert isinstance(visto.get("exc"), RuntimeError)


def test_una_bitacora_ROTA_no_rompe_la_guarda(monkeypatch):
    """`errores.registrar` promete no levantar, pero la guarda NO puede depender de esa
    promesa: si algun dia rompe, se lleva puesto el manejo del fallo original y con el las
    alertas. Misma cautela que `worker._registrar_fallo` del lado del llamador."""
    from src import errores

    def revienta(cur):
        raise RuntimeError("permission denied")

    def bitacora_rota(*a, **k):
        raise RuntimeError("la tabla errors no existe")

    monkeypatch.setattr(desconexiones, "ensure_table", revienta)
    monkeypatch.setattr(errores, "registrar", bitacora_rota)
    cur = _FakeCursor()
    assert desconexiones.asegurar_sin_romper(cur, log=lambda m: None) is False
    assert any("ROLLBACK TO SAVEPOINT" in s for s in cur.sql)


def test_alertas_ensure_table_usa_la_version_que_NO_rompe(monkeypatch):
    """La guarda no sirve si el cableado llama a la version estricta."""
    from src import alertas

    def revienta(cur):
        raise RuntimeError("permission denied for table users")

    monkeypatch.setattr(desconexiones, "ensure_table", revienta)
    alertas.ensure_table(_FakeCursor())   # no tiene que levantar


def test_el_DDL_lo_corre_ALGUIEN_en_produccion():
    """UN DDL QUE NADIE LLAMA ES UN ARCHIVO, NO UNA TABLA. `alertas.ensure_table` corre en
    cada ciclo del worker, asi que la captura arranca con el primer ciclo. Sin este cableo
    todo lo de arriba pasa los tests y no existe en la base."""
    from src import alertas

    cur = _FakeCursor()
    alertas.ensure_table(cur)
    junto = " ".join(cur.sql)
    assert "conexiones_operador" in junto
    assert "trg_conexiones_operador" in junto


def test_el_indice_sirve_a_la_consulta_de_la_alerta():
    """La consulta que viene es 'desconexiones de una cuenta, lo mas reciente primero'.
    Indice parcial sobre offline, mismo idioma que `idx_operator_status_off`."""
    ddl = _ddl()
    assert "CREATE INDEX IF NOT EXISTS" in ddl
    assert "offline" in ddl


# =====================================================================
# LA ALERTA: el drenaje del outbox
# =====================================================================

def test_la_clave_de_la_alerta_es_el_EVENTO_y_no_el_OPERADOR():
    """Un operador se desconecta muchas veces. Dedupear por operador dejaria muda la
    segunda desconexion y todas las que siguen; dedupear por `(operador, fecha)` haria lo
    mismo dentro del dia. La clave es la fila del historial, que es UN evento.

    Mismo razonamiento que `alertas.clave_resumen`, que paso de sesion a interaccion por
    dejar mudas N-1 atenciones."""
    assert desconexiones.clave_alerta(41) == "41"
    assert desconexiones.clave_alerta(41) != desconexiones.clave_alerta(42)


def test_solo_alerta_DESCONEXIONES_no_reconexiones():
    """El historial guarda las dos transiciones. Avisar de una reconexion no es una alerta,
    es ruido con el doble de volumen."""
    assert "status_nuevo = 'offline'" in desconexiones._PENDIENTES_SQL


def test_no_alerta_a_los_operadores_APAGADOS_y_matchea_por_CLAVE():
    """Un operador con baja logica en `operator_status` no esta trabajando: su desconexion
    no le importa a nadie.

    Y EL MATCH VA POR CLAVE, no por string exacto. Es el bug que ya se pago con
    `operator_status` el 2026-08-27: la tabla tenia 'RAMIREZ', el modal mandaba 'Ramirez',
    el ON CONFLICT no matcheaba y el operador no se prendia NUNCA. Ver
    `identidad.clave_sql`."""
    sql = desconexiones._PENDIENTES_SQL
    assert "operator_status" in sql
    assert "activo" in sql
    assert "lower" in sql.lower() or "translate" in sql.lower(), \
        "el match contra operator_status tiene que ser por clave, no por string exacto"


def test_la_consulta_tiene_VENTANA_y_TOPE():
    """LA LECCION DEL APAGON DE HOY. El worker estuvo 93 minutos caido; cuando vuelve, sin
    ventana dispara TODAS las desconexiones acumuladas de un saque. Y el sembrado de
    `ledger_vacio` no cubre esto: solo protege el PRIMER arranque de la historia, no cada
    reinicio.

    El tope por ciclo es la segunda red: un pico no puede volcar cien mensajes seguidos."""
    assert "detected_at >" in desconexiones._PENDIENTES_SQL
    assert desconexiones.VENTANA_ALERTA_MINUTOS > 0
    assert desconexiones.TOPE_POR_CICLO > 0


# --- la consulta trae emparejada la SESION que se cierra con la desconexion -

def test_la_sql_de_pendientes_pairea_la_sesion_con_LAG_particionado():
    """El emparejado va en SQL, no en Python -- mismo patron que `_SESIONES_SQL`. La
    particion por (user_id, account) es lo que impide que el `online` anterior de OTRO
    operador o de OTRA cuenta se cuele en esta desconexion."""
    sql = desconexiones._PENDIENTES_SQL
    assert "lag(" in sql.lower()
    assert "partition by user_id, account" in sql.lower()


def test_la_sql_de_pendientes_solo_expone_la_sesion_si_el_ANTERIOR_fue_online():
    """Si el evento inmediato anterior NO fue un `online` (otro `offline`, o no hay
    anterior -- 11 de 22 operadores en produccion arrancan asi su historial), la sesion NO
    EXISTE: tiene que dar NULL, no el dato crudo de ese evento anterior sea cual sea."""
    sql = desconexiones._PENDIENTES_SQL
    assert "case when" in sql.lower()
    assert "= 'online'" in sql


def test_el_mensaje_va_en_HTML_ESCAPADO():
    """Los nombres vienen del CRM. `_` y `&` en un nombre ya rompieron un envio real con
    400 can't parse entities (ver `Canal.enviar`)."""
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Ana & Co <b>", "account": "datos",
        "last_seen": None, "detected_at": None,
    })
    # LA PROPIEDAD, NO SU HUELLA: el dato crudo NO puede aparecer tal cual, y sus tres
    # caracteres peligrosos tienen que estar escapados. Buscar `"<b>Ana" not in msg` era
    # una asercion MAL escrita: ese `<b>` es la negrita del propio mensaje, no inyeccion --
    # el mismo error de confundir un string con la propiedad que se queria probar.
    assert "Ana & Co <b>" not in msg, "el dato crudo llego sin escapar"
    assert "&amp;" in msg and "&lt;b&gt;" in msg


def test_el_mensaje_dice_QUIEN_y_CUANDO_en_hora_local():
    """Un aviso que no dice la hora local obliga a hacer la cuenta de UTC-5 a mano."""
    from datetime import datetime, timedelta, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Arturo", "account": "datos",
        "last_seen": datetime(2026, 9, 2, 20, 1, 29, tzinfo=utc),
        "detected_at": datetime(2026, 9, 2, 20, 3, 0, tzinfo=utc),
    })
    assert "Arturo" in msg
    assert "datos" in msg
    assert "15:01" in msg, "la hora tiene que ir en Ecuador (UTC-5), no en UTC"


def test_el_mensaje_sin_nombre_no_imprime_None():
    msg = desconexiones.mensaje_desconexion({
        "operator_name": None, "account": "datos", "last_seen": None, "detected_at": None})
    assert "None" not in msg


_VIP = {"TELEGRAM_TOKEN_VIP": "t", "TELEGRAM_CHAT_VIP": "c"}


def test_va_al_MISMO_chat_que_las_alertas_VIP():
    """Decision del negocio (2026-09-03): un solo grupo. El aviso tiene otro proposito, no
    otro destino. Asi que el canal SALE de `alertas.canal_desde_env`, sin variables propias:
    dos pares de variables para el mismo chat se desincronizan el dia que se rote el token."""
    canal = desconexiones.canal_desde_env({**_VIP, "ALERTA_DESCONEXION": "true"})
    assert canal.configurado
    from src import alertas
    assert canal == alertas.canal_desde_env(_VIP), "tiene que ser EL MISMO canal, no una copia"


def test_el_INTERRUPTOR_es_propio_y_arranca_APAGADO():
    """EL CANAL ES COMPARTIDO, EL INTERRUPTOR NO. Y esto no es celo: el canal VIP YA esta
    configurado en produccion, asi que sin flag esta alerta se prende sola en el momento del
    deploy -- con un volumen estimado de 50 a 150 avisos por dia que todavia NO se midio.

    El historial se llena igual con la alerta apagada, asi que el volumen se mide sobre
    datos propios y despues se prende. Mismo patron opt-in que `SCORING_ENABLED` y
    `API_DOCS`: el default tiene que ser el seguro, no el comodo."""
    assert not desconexiones.canal_desde_env(_VIP).configurado, \
        "con el canal VIP puesto y SIN el flag tiene que estar APAGADO"
    assert not desconexiones.canal_desde_env({"ALERTA_DESCONEXION": "true"}).configurado, \
        "con el flag pero sin canal no hay a donde mandar"
    assert desconexiones.canal_desde_env(
        {**_VIP, "ALERTA_DESCONEXION": "1"}).configurado


def test_el_mensaje_es_INCONFUNDIBLE_en_un_chat_compartido():
    """COMPARTIR CHAT TIENE UNA CONSECUENCIA: quien lee el grupo tiene que distinguir de un
    vistazo un aviso de desconexion de un resumen VIP, sin abrir nada. Los resumenes abren
    con 🍀 y la marca de espera con ⏳; este abre con 🔌 y dice de que es en la PRIMERA
    linea, que es lo unico que se ve en la notificacion del telefono."""
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Arturo", "account": "datos",
        "last_seen": None, "detected_at": None})
    primera = msg.splitlines()[0]
    assert "🔌" in primera
    assert "esconecta" in primera, "la primera linea tiene que decir QUE es"
    assert "🍀" not in msg and "⏳" not in msg, "no puede confundirse con un resumen VIP"


# --- la linea de la SESION que se cierra con esta desconexion ---------------
#
# LA REGLA DEL NEGOCIO (decidida por el usuario, sin margen para interpretarla): la sesion es
# el `online` INMEDIATO ANTERIOR del MISMO (user_id, account). Si no hay uno, la linea NO SE
# AGREGA -- ni "desconocido", ni una estimacion desde el arranque de la captura. Medido el
# 2026-09-03: 11 de 22 operadores arrancan su historial con un `offline` porque ya estaban
# conectados cuando el trigger empezo a grabar (17:03 UTC); esos NO tienen sesion que contar,
# y el mensaje tiene que salir IDENTICO al de hoy.

def test_el_mensaje_agrega_la_linea_de_sesion_cuando_HAY_online_previo():
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Alejandra", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "sesion_last_seen_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
    })
    assert "9 min" in msg, "9 minutos de sesion (17:00 a 17:09) no puede faltar"
    assert "12:00:00" in msg, "el inicio va en hora Ecuador: 17:00 UTC - 5h = 12:00:00"


def test_el_mensaje_NO_agrega_linea_de_sesion_sin_online_previo():
    """El caso de 11 de 22 operadores en produccion: el `offline` es el primer evento de su
    historial. El mensaje tiene que salir EXACTAMENTE igual al de hoy, sin la linea de mas."""
    from datetime import datetime, timezone

    utc = timezone.utc
    base = {
        "operator_name": "Bryan", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
    }
    con_sesion_nula = {**base,
                        "sesion_last_seen_inicio": None, "sesion_detected_at_inicio": None}
    msg_sin_claves = desconexiones.mensaje_desconexion(base)
    assert msg_sin_claves == desconexiones.mensaje_desconexion(con_sesion_nula)
    assert "🕓" not in msg_sin_claves, "sin online previo no hay linea de sesion"


def test_la_duracion_de_sesion_usa_LAST_SEEN_y_no_DETECTED_AT():
    """Mismo argumento que `sesiones_cerradas`: la latencia mediana del sync es 157 s en
    offline y 151 s en online (maximo ~5 min). Con ambos relojes presentes y DISTINTOS la
    duracion tiene que salir del de `last_seen`."""
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Carla", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 11, 37, tzinfo=utc),        # +157s latencia
        "sesion_last_seen_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 16, 57, 29, tzinfo=utc),  # -151s
    })
    assert "9 min" in msg, "17:00 a 17:09 por last_seen son 9 minutos, no por detected_at"


def test_la_duracion_de_sesion_cae_a_DETECTED_AT_si_LAST_SEEN_es_NULL():
    """`last_seen` es NULLABLE en el esquema. El fallback es POR PUNTA: si falta en el
    arranque de la sesion cae a `detected_at` de esa misma punta -- consistente con
    `sesiones_cerradas`."""
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Diego", "account": "datos",
        "last_seen": None,
        "detected_at": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "sesion_last_seen_inicio": None,
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
    })
    assert "9 min" in msg


def test_la_linea_de_sesion_usa_HORAS_cuando_pasa_de_60_minutos():
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Elena", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 20, 12, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 20, 12, 0, tzinfo=utc),
        "sesion_last_seen_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
    })
    assert "3 h 12 min" in msg


def test_la_linea_de_sesion_dice_TIEMPO_DE_SESION_y_no_CONECTADO():
    """Pedido del usuario, textual: "en la de desconexion en vez de conexion ponle tiempo de
    sesion para que quede mas claro". Solo cambia la etiqueta -- la duracion, el fallback y
    el resto del mensaje siguen igual (ver los tests de arriba)."""
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Gabriela", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "sesion_last_seen_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
    })
    assert "🕓 tiempo de sesion 9 min (desde 12:00:00)" in msg
    assert "🕓 conectado" not in msg, "la etiqueta vieja no puede seguir apareciendo"


def test_la_linea_de_LATENCIA_sigue_funcionando_con_la_linea_de_sesion():
    """La linea nueva no puede romper el comportamiento ya probado de `⏱ detectado N min
    despues`: las dos tienen que poder convivir en el mismo mensaje, cada una con su propio
    dato -- la sesion sale de `last_seen`, la latencia se mide contra `detected_at`."""
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_desconexion({
        "operator_name": "Fabian", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 9, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 20, 0, tzinfo=utc),   # 11 min de latencia
        "sesion_last_seen_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "sesion_detected_at_inicio": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
    })
    assert "⏱ detectado 11 min despues" in msg
    assert "9 min" in msg, "la sesion sigue durando 9 minutos, sin cambios"


# =====================================================================
# LA SONDA DE ARRANQUE
# =====================================================================

class _CursorEstado:
    """Cursor falso para la sonda: contesta las dos consultas por orden."""

    def __init__(self, primera, segunda=(0, None), duenos=True):
        self._primera, self._segunda, self._duenos = primera, segunda, duenos
        self._n = 0

    def execute(self, sql, params=None):
        txt = " ".join(str(sql).split())
        # `FROM conexiones_operador` y no `count(*)`: _ESTADO_SQL tambien tiene un
        # `count(*)` (sobre pg_trigger) y menciona la tabla, asi que el despacho por esas
        # dos subcadenas matcheaba la PRIMERA consulta. Otra vez una huella textual
        # demasiado ancha.
        self._ultimo = ("duenos" if "tgfoid" in txt
                        else "conteo" if "FROM conexiones_operador" in txt
                        else "primera")
    def fetchone(self):
        if self._ultimo == "duenos":
            return (self._duenos,)
        if self._ultimo == "conteo":
            return self._segunda
        return self._primera
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _ConnEstado:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _estado(primera, segunda=(0, None), duenos=True) -> str:
    cur = _CursorEstado(primera, segunda, duenos)
    return desconexiones.estado("dsn", connect=lambda: _ConnEstado(cur))


def test_la_sonda_NUNCA_levanta_y_devuelve_UNA_linea():
    """Es un log de arranque: no puede impedir que el worker levante. Mismo contrato que
    `errores.estado`."""
    def revienta():
        raise RuntimeError("could not connect to server")

    linea = desconexiones.estado("dsn", connect=revienta)
    assert isinstance(linea, str) and "\n" not in linea
    assert "could not connect" in linea


def test_la_sonda_distingue_el_deploy_que_ENTRO_del_que_NO():
    """La razon de existir de esto, igual que en `errores.estado`: el orden de deploy y los
    privilegios no se pueden deducir del codigo. UNA linea que separe "esta capturando" de
    "el trigger no esta y por eso"."""
    # (trigger, definer, puede_trigger, tabla)
    assert "NO ACTIVA" in _estado((0, None, False, False))
    # Y NO PUEDE NORMALIZAR LA FALLA. La primera version decia "(la crea el worker en su
    # primer ciclo)", porque la sonda corria ANTES de que el hilo de alertas hubiera tenido
    # su primer ciclo: informaba "falta la tabla" en CADA arranque. Se vio en produccion el
    # 2026-09-03 16:31:49. Era yo tapando un bug de orden con texto tranquilizador, que es
    # justo lo que entrena a la gente a ignorar la linea. Ahora se asegura ANTES de sondear,
    # asi que una tabla ausente aca es una falla REAL y el mensaje tiene que apuntar a ella.
    assert "primer ciclo" not in _estado((0, None, False, False))
    assert "privilegio TRIGGER" in _estado((0, None, False, True))
    assert "SECURITY DEFINER" in _estado((1, False, True, True))
    assert "dueño" in _estado((1, True, True, True), duenos=False)


def test_la_sonda_dice_CUANTOS_eventos_y_hace_cuanto():
    """`lista` sin numeros no distingue "capturando" de "el trigger esta y no inserta" --
    que es el modo de falla que costo dos rondas encontrar."""
    from datetime import timedelta

    linea = _estado((1, True, True, True), segunda=(41, timedelta(minutes=3)))
    assert "41" in linea and ("0:03" in linea or "3" in linea)


def test_la_sonda_no_asusta_en_un_despliegue_NUEVO():
    """Cero eventos recien desplegado es NORMAL: nadie se desconecto todavia. Decir "roto"
    ahi entrena a la gente a ignorar la linea."""
    linea = _estado((1, True, True, True), segunda=(0, None))
    assert "0 eventos" in linea
    assert "ROTA" not in linea and "NO ACTIVA" not in linea


# =====================================================================
# LAS SESIONES DE CONEXION: el ciclo cerrado online -> offline
# =====================================================================
#
# LA REGLA DEL NEGOCIO (decidida por el usuario, no negociable): una sesion es un `online`
# emparejado con el SIGUIENTE `offline` del mismo (user_id, account). Si un `online` todavia
# no tiene su `offline`, esa sesion NO EXISTE -- no se estima "tiempo hasta ahora", no se
# emite fila parcial. "No hay pierde, no podemos hacer mas."
#
# POR QUE `last_seen` Y NO `detected_at`. `detected_at` es cuando el SYNC se dio cuenta, no
# cuando paso. Medido el 2026-09-03 sobre 58 filas: la latencia mediana es 157 s en offline y
# 151 s en online, con un maximo de ~5 minutos. En la sesion corta de Alejandra la diferencia
# fue de 5 minutos por `detected_at` contra 9 minutos por `last_seen`: un error del 80% justo
# en las sesiones cortas, que son las que le importan al negocio. `last_seen` es NULL en la
# copia de hoy en 0 de 58 filas, pero la columna lo permite, asi que el fallback a
# `detected_at` se prueba igual.
#
# EL EMPAREJADO VA EN SQL, NO EN PYTHON. `lead()` particionado por `(user_id, account)` ve
# TODA la secuencia de una sola pasada; reimplementarlo con un loop en Python duplicaria una
# logica que el motor ya resuelve de forma correcta y mas barata.
#
# QUE SE PRUEBA DESDE PYTHON Y QUE NO. Igual que el resto del modulo (ver el encabezado del
# archivo): este repo no tiene tests contra una base real. El EMPAREJADO (`lead()`, el
# `WHERE status_sig = 'offline'` que descarta al operador todavia conectado) se prueba por el
# TEXTO de la consulta. El FALLBACK `last_seen` -> `detected_at` y el CALCULO de la duracion
# son Python puro sobre las columnas que la consulta ya deja pareadas, y esos SI se prueban
# con valores reales.

class _FakeCursorSesiones:
    """Cursor falso con `description` + `fetchall`, para una funcion que arma dicts a partir
    de columnas nombradas. Mismo patron que `tests/test_queries.py::_FakeCursor`: las filas
    que devuelve `fetchall` son las que la consulta YA pareo via `lead()` -- lo que no se
    puede correr desde aca es el propio `lead()`, que se prueba por texto (ver abajo)."""

    def __init__(self, rows=(), description=()):
        self._rows = rows
        self.description = [type("C", (), {"name": n})() for n in description]
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchall(self):
        return self._rows


_COLS_SESIONES = ("user_id", "account", "operator_name",
                   "last_seen_inicio", "detected_at_inicio",
                   "last_seen_fin", "detected_at_fin")


def test_un_ciclo_cerrado_devuelve_UNA_sesion():
    """El caso normal: un online con su offline, ambos relojes presentes."""
    from datetime import datetime, timezone

    ini = datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc)
    fin = datetime(2026, 9, 3, 17, 9, 0, tzinfo=timezone.utc)
    cur = _FakeCursorSesiones(
        rows=[("u1", "datos", "Alejandra", ini, ini, fin, fin)],
        description=_COLS_SESIONES,
    )
    sesiones = desconexiones.sesiones_cerradas(cur)
    assert len(sesiones) == 1
    s = sesiones[0]
    assert s["account"] == "datos"
    assert s["user_id"] == "u1"
    assert s["operator_name"] == "Alejandra"
    assert s["start"] == ini
    assert s["end"] == fin
    assert s["duration_seconds"] == 540


def test_el_operador_TODAVIA_conectado_no_aparece():
    """Si el ultimo evento de un operador es un `online` sin `offline` despues, `lead()` da
    NULL para esa fila y el `WHERE status_sig = 'offline'` la descarta antes de que llegue a
    Python. No hay pierde: no se estima tiempo contra el reloj actual."""
    sql = desconexiones._SESIONES_SQL
    assert "status_sig = 'offline'" in sql
    assert "now()" not in sql, "no se estima 'tiempo hasta ahora' para una sesion abierta"

    cur = _FakeCursorSesiones(rows=[], description=_COLS_SESIONES)
    assert desconexiones.sesiones_cerradas(cur) == [], \
        "sin filas de la consulta (operador todavia online), no hay sesiones"


def test_varios_ciclos_consecutivos_del_MISMO_operador():
    """Dos ciclos completos del mismo operador tienen que salir como DOS sesiones
    independientes, cada una con su propia duracion."""
    from datetime import datetime, timezone

    def t(h, m):
        return datetime(2026, 9, 3, h, m, 0, tzinfo=timezone.utc)

    filas = [
        ("u1", "datos", "Bryan", t(8, 0), t(8, 0), t(8, 5), t(8, 5)),
        ("u1", "datos", "Bryan", t(9, 0), t(9, 0), t(9, 20), t(9, 20)),
    ]
    cur = _FakeCursorSesiones(rows=filas, description=_COLS_SESIONES)
    sesiones = desconexiones.sesiones_cerradas(cur)
    assert len(sesiones) == 2
    assert sesiones[0]["duration_seconds"] == 300
    assert sesiones[1]["duration_seconds"] == 1200


def test_dos_operadores_intercalados_no_se_mezclan():
    """El emparejado parte por `(user_id, account)`: dos operadores con eventos intercalados
    en el tiempo no pueden cruzarse las sesiones entre si."""
    from datetime import datetime, timezone

    def t(h, m):
        return datetime(2026, 9, 3, h, m, 0, tzinfo=timezone.utc)

    filas = [
        ("u1", "datos", "Ana", t(8, 0), t(8, 0), t(8, 10), t(8, 10)),
        ("u2", "datos", "Bruno", t(8, 3), t(8, 3), t(8, 8), t(8, 8)),
    ]
    cur = _FakeCursorSesiones(rows=filas, description=_COLS_SESIONES)
    sesiones = desconexiones.sesiones_cerradas(cur)
    por_nombre = {s["operator_name"]: s for s in sesiones}
    assert set(por_nombre) == {"Ana", "Bruno"}
    assert por_nombre["Ana"]["duration_seconds"] == 600
    assert por_nombre["Bruno"]["duration_seconds"] == 300
    assert "PARTITION BY user_id, account" in desconexiones._SESIONES_SQL, \
        "sin particionar por (user_id, account) dos operadores se pueden cruzar"


def test_last_seen_NULL_usa_detected_at():
    """`last_seen` es NULLABLE en el esquema. Hoy esta lleno en 58/58 filas de la copia, pero
    el fallback tiene que existir y probarse igual: sin el, una fila con `last_seen` NULL
    rompe con TypeError al restar `None - None`."""
    from datetime import datetime, timezone

    d_ini = datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc)
    d_fin = datetime(2026, 9, 3, 17, 5, 0, tzinfo=timezone.utc)
    cur = _FakeCursorSesiones(
        rows=[("u1", "datos", "Carla", None, d_ini, None, d_fin)],
        description=_COLS_SESIONES,
    )
    s = desconexiones.sesiones_cerradas(cur)[0]
    assert s["start"] == d_ini
    assert s["end"] == d_fin
    assert s["duration_seconds"] == 300


def test_la_duracion_usa_LAST_SEEN_y_no_DETECTED_AT():
    """LA DECISION CRITICA, medida el 2026-09-03: la latencia del sync es de mediana 157 s en
    offline y 151 s en online (maximo ~5 min). En la sesion corta de Alejandra la diferencia
    fue de 5 minutos por `detected_at` contra 9 minutos por `last_seen` -- un error del 80%
    justo en las sesiones cortas, que son las que le importan al negocio. Con ambos relojes
    presentes y DISTINTOS, la duracion tiene que salir del de `last_seen`."""
    from datetime import datetime, timezone

    ls_ini = datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc)
    da_ini = datetime(2026, 9, 3, 17, 2, 31, tzinfo=timezone.utc)   # +151 s de latencia
    ls_fin = datetime(2026, 9, 3, 17, 9, 0, tzinfo=timezone.utc)
    da_fin = datetime(2026, 9, 3, 17, 11, 37, tzinfo=timezone.utc)  # +157 s de latencia
    cur = _FakeCursorSesiones(
        rows=[("u1", "datos", "Alejandra", ls_ini, da_ini, ls_fin, da_fin)],
        description=_COLS_SESIONES,
    )
    s = desconexiones.sesiones_cerradas(cur)[0]
    assert s["duration_seconds"] == 540, "9 minutos por last_seen, no por detected_at"


def test_la_sql_pairea_con_LEAD_y_no_en_Python():
    """El emparejado online/offline lo hace el motor con una window function, no un loop en
    Python que reimplemente lo mismo mas caro y con mas superficie de bug."""
    sql = desconexiones._SESIONES_SQL
    assert "lead(" in sql.lower()
    assert "partition by user_id, account" in sql.lower()
    assert "status_nuevo = 'online'" in sql
    assert "status_sig = 'offline'" in sql


# =====================================================================
# LA ALERTA DE CONEXION: el otro lado del mismo ciclo
# =====================================================================
#
# NO LLEVA DURACION DE SESION, A PROPOSITO: al momento de conectar la sesion todavia no
# cerro, no hay nada que resumir -- la duracion aparece recien cuando el ciclo se cierra, en
# el aviso de desconexion. La clave de dedup es la MISMA regla que en desconexiones
# (`clave_alerta`, el evento y no el operador): se reusa la funcion, no se reimplementa.

def test_la_sql_de_conexion_filtra_ONLINE_y_dedupea_por_tipo_conexion():
    """Espejo de `test_solo_alerta_DESCONEXIONES_no_reconexiones`, pero para el otro lado del
    ciclo: la conexion no puede colarse en el dedup de la desconexion ni viceversa."""
    sql = desconexiones._PENDIENTES_CONEXION_SQL
    assert "status_nuevo = 'online'" in sql
    assert "tipo    = 'conexion'" in sql or "tipo = 'conexion'" in sql


def test_la_sql_de_conexion_excluye_operadores_APAGADOS_y_matchea_por_CLAVE():
    """Misma guarda que `pendientes`: un operador con baja logica en `operator_status` no
    esta trabajando, y el match va por clave (`identidad.clave_sql`) y no por string exacto."""
    sql = desconexiones._PENDIENTES_CONEXION_SQL
    assert "operator_status" in sql
    assert "activo" in sql
    assert "lower" in sql.lower() or "translate" in sql.lower()


def test_la_sql_de_conexion_tiene_VENTANA_y_TOPE():
    """Misma leccion del apagon que la de desconexion: sin ventana, un worker que vuelve
    dispara todas las conexiones acumuladas de un saque."""
    sql = desconexiones._PENDIENTES_CONEXION_SQL
    assert "detected_at >" in sql
    assert "LIMIT %(tope)s" in sql


def test_la_sql_de_conexion_NO_pairea_sesion():
    """La conexion no tiene ciclo que cerrar: no hace falta `lag()`/`lead()` ni una
    subconsulta con window function, a diferencia de `_PENDIENTES_SQL` (desconexion)."""
    sql = desconexiones._PENDIENTES_CONEXION_SQL
    assert "lag(" not in sql.lower()
    assert "lead(" not in sql.lower()
    assert "window" not in sql.lower()


def test_la_sql_de_conexion_y_la_de_desconexion_comparten_el_bloque_de_guardas():
    """Factorizado, no copiado y pegado: el dedup, la ventana, el tope y la exclusion de
    apagados son el MISMO texto en las dos consultas salvo el tipo/estado -- ver
    `_bloque_guardas_pendientes`. Si una consulta cambia esa parte y la otra no, este test
    revienta."""
    off = desconexiones._PENDIENTES_SQL
    on = desconexiones._PENDIENTES_CONEXION_SQL
    comun = desconexiones._bloque_guardas_pendientes("desconexion", "offline")
    assert comun in off
    comun_on = desconexiones._bloque_guardas_pendientes("conexion", "online")
    assert comun_on in on


def test_pendientes_conexion_ejecuta_con_los_MISMOS_parametros_que_pendientes():
    """Cuenta, ventana y tope entran igual que en `pendientes`: mismo contrato para que el
    worker pueda llamar a las dos funciones de forma simetrica."""
    from datetime import datetime, timezone

    class _Cursor:
        def __init__(self):
            self.params = None
            self.description = [type("C", (), {"name": n})()
                                for n in ("id", "account", "operator_name",
                                          "last_seen", "detected_at")]

        def execute(self, sql, params=None):
            self.params = params

        def fetchall(self):
            return [(1, "datos", "Hugo",
                     datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc),
                     datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc))]

    cur = _Cursor()
    ahora = datetime(2026, 9, 3, 17, 30, 0, tzinfo=timezone.utc)
    filas = desconexiones.pendientes_conexion(cur, "datos", ahora, ventana_minutos=30, tope=5)
    assert cur.params["account"] == "datos"
    assert cur.params["tope"] == 5
    assert len(filas) == 1
    assert filas[0]["operator_name"] == "Hugo"


def test_la_clave_de_conexion_reusa_clave_alerta():
    """La misma regla que en desconexiones: la clave es el EVENTO, no el operador. No hay
    una `clave_alerta_conexion` aparte -- `clave_alerta` ya es generica sobre el id."""
    assert desconexiones.clave_alerta(77) == "77"


def test_el_mensaje_de_conexion_dice_QUIEN_CUENTA_y_HORA_local():
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_conexion({
        "operator_name": "Ines", "account": "sistemas",
        "last_seen": datetime(2026, 9, 3, 20, 1, 29, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 20, 3, 0, tzinfo=utc),
    })
    assert "Ines" in msg
    assert "sistemas" in msg
    assert "15:01" in msg, "la hora tiene que ir en Ecuador (UTC-5), no en UTC"


def test_el_mensaje_de_conexion_NO_lleva_duracion_de_sesion():
    """A DIFERENCIA de la desconexion: al conectar la sesion todavia no cerro, no hay nada
    que resumir. Ni la palabra 'sesion' ni el simbolo de la linea de duracion (🕓) pueden
    aparecer."""
    msg = desconexiones.mensaje_conexion({
        "operator_name": "Julian", "account": "datos",
        "last_seen": None, "detected_at": None})
    assert "🕓" not in msg
    assert "sesion" not in msg.lower()


def test_el_mensaje_de_conexion_es_ESCAPADO_y_sin_None():
    msg = desconexiones.mensaje_conexion({
        "operator_name": "Ana & Co <b>", "account": "datos",
        "last_seen": None, "detected_at": None,
    })
    assert "Ana & Co <b>" not in msg
    assert "&amp;" in msg and "&lt;b&gt;" in msg

    msg_sin_nombre = desconexiones.mensaje_conexion({
        "operator_name": None, "account": "datos", "last_seen": None, "detected_at": None})
    assert "None" not in msg_sin_nombre


def test_el_mensaje_de_conexion_LATENCIA_sigue_funcionando():
    """La linea `⏱ detectado N min despues` aplica identica: la latencia medida en `online`
    es de mediana 151 s."""
    from datetime import datetime, timezone

    utc = timezone.utc
    msg = desconexiones.mensaje_conexion({
        "operator_name": "Karen", "account": "datos",
        "last_seen": datetime(2026, 9, 3, 17, 0, 0, tzinfo=utc),
        "detected_at": datetime(2026, 9, 3, 17, 2, 31, tzinfo=utc),   # +151 s
    })
    assert "⏱ detectado 2 min despues" in msg


def test_el_mensaje_de_conexion_es_INCONFUNDIBLE_en_el_chat_compartido():
    """Marcador verde en vez del enchufe -- distinguible de un vistazo de la desconexion, de
    los resumenes VIP (🍀) y de la alerta de espera (⏳)."""
    msg = desconexiones.mensaje_conexion({
        "operator_name": "Karen", "account": "datos",
        "last_seen": None, "detected_at": None})
    primera = msg.splitlines()[0]
    assert "🟢" in primera
    assert "onectad" in primera, "la primera linea tiene que decir QUE es"
    assert "🔌" not in msg and "🍀" not in msg and "⏳" not in msg


def test_el_indice_de_conexion_es_NUEVO_e_IDEMPOTENTE_y_no_toca_el_de_offline():
    """El indice parcial existente es `WHERE status_nuevo = 'offline'` y no sirve para
    buscar `online`: hace falta uno segundo, propio, sin tocar el primero -- ya esta
    desplegado."""
    ddl = _ddl()
    assert ddl.count("CREATE INDEX IF NOT EXISTS") == 2
    assert "WHERE status_nuevo = 'offline'" in ddl
    assert "WHERE status_nuevo = 'online'" in ddl
    # el indice de offline sigue apareciendo LITERAL, sin alterar: no se dropea ni se
    # renombra.
    assert "idx_conexiones_operador_off" in ddl


def test_el_flag_ALERTA_DESCONEXION_es_el_MISMO_para_las_dos_mitades_del_ciclo():
    """Decision registrada en el docstring de `canal_desde_env`: conexion y desconexion son
    las dos mitades de UN ciclo, y un flag separado dejaria prender la mitad -- sin agregar
    una variable de entorno nueva."""
    assert desconexiones.canal_desde_env({**_VIP, "ALERTA_DESCONEXION": "true"}).configurado
    assert not desconexiones.canal_desde_env(_VIP).configurado
