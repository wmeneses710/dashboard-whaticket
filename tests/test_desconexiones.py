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


def test_el_canal_de_desconexiones_es_PROPIO_y_arranca_APAGADO():
    """Canal aparte del de VIP: otra audiencia (supervision de ATC, no quien mira VIP) y,
    sobre todo, un interruptor propio. Vacio = apagado, asi que se puede desplegar el
    codigo y medir el volumen en `conexiones_operador` ANTES de prender el aviso."""
    canal = desconexiones.canal_desde_env({})
    assert not canal.configurado, "sin variables tiene que arrancar APAGADO"
    canal = desconexiones.canal_desde_env(
        {"TELEGRAM_TOKEN_DESCONEXION": "t", "TELEGRAM_CHAT_DESCONEXION": "c"})
    assert canal.configurado
    # y NO puede reusar las del canal VIP: prenderia solo, sin que nadie lo decida
    assert not desconexiones.canal_desde_env(
        {"TELEGRAM_TOKEN_VIP": "t", "TELEGRAM_CHAT_VIP": "c"}).configurado
