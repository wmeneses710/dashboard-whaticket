"""Historial de conexion/desconexion de operadores. LA CAPTURA, no la alerta.

QUE PROBLEMA RESUELVE. `users` es un SNAPSHOT: el ETL pisa `status`, `last_seen` y
`refreshed_at` en cada sync y el estado anterior DESAPARECE. Medido el 2026-09-03: la
tabla tiene un solo `refreshed_at` por cuenta (15 usuarios a las 14:23:59, 30 a las
14:21:10), o sea que se reescribe entera de un saque. Consecuencia practica: a la
pregunta "quien se desconecto ayer a las 3pm" solo se le puede contestar por los que NO
volvieron a conectarse; el que se fue y regreso es invisible para siempre.

POR QUE UN TRIGGER Y NO UN POLLER. La alternativa era leer `users` cada 60 s desde el
worker y comparar. Pierde eventos: si el ETL escribe dos veces dentro de la misma
ventana, la transicion intermedia no existio nunca para nosotros. El trigger ve TODOS los
cambios por construccion, y ademas es agnostico del escritor -- funciona con el ETL de
hoy, con un UPDATE a mano, y con el ETL reescrito.

POR QUE EL TRIGGER NO MANDA LA ALERTA. Porque correria dentro de la transaccion del ETL:
un `sendMessage` a Telegram dejaria la transaccion abierta esperando red, y un corte de
Telegram frenaria la ingesta. Ademas un envio no se puede deshacer, asi que un rollback
posterior dejaria una alerta por un evento que nunca se commiteo. El trigger escribe la
fila (transaccional, sin red) y el hilo de alertas que ya corre cada 60 s la drena con
`Canal.enviar` + `marcar_enviada` (patron outbox). Cada mitad falla sola.

QUIEN ESCRIBE ESTE DDL. Lo asegura el dashboard aunque el dueño de `users` sea el ETL,
por el mismo motivo que `alertas.ensure_table` crea `vip_players`: el orden de deploy no
se puede deducir del codigo (ver `errores.estado`, donde el dashboard subiendo antes que
el `db/schema.sql` del ETL dejaba la tabla vacia EN SILENCIO). Las CINCO sentencias son
re-ejecutables --`IF NOT EXISTS`, `OR REPLACE` y un `REVOKE`, que revocando algo ya
revocado es un no-op--, asi que correrlo repetido es inofensivo y el trigger se
restablece solo si un redeploy del ETL recrea `users`.

LO QUE TODAVIA NO RESUELVE: la CADENCIA. El trigger garantiza que no se pierda ningun
cambio que llegue, pero dispara cuando el ETL escribe. Son dos perillas distintas y esta
es solo la de la captura.

LA CADENCIA MEDIDA ES ~300 s (`LOOKUP_REFRESH_SECONDS` del ETL): `sistemas` 1,02 s y
`datos` 238 s de `refreshed_at - max(last_seen)` el 2026-09-03. Una version anterior de
este comentario decia "una vez por dia", inferido de dos lecturas de `refreshed_at`
separadas 24 h. Esos dos puntos NO median la cadencia: `refreshed_at` se pisa entero en
cada pasada, asi que con 5 minutos o con 24 horas el snapshot se ve IDENTICO -- estaban
separados 24 h porque se consulto a la misma hora del dia. La metrica era incapaz de
distinguir la hipotesis de su alternativa; el par `last_seen`/`refreshed_at` si puede.

VOLUMEN: 49 operadores por unas pocas transiciones diarias. Decenas de filas por dia, no
hace falta politica de retencion.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Nombres fijos, no generados: `alertas.ensure_table` es el unico llamador y corre una vez
# por cuenta, sin anidamiento. UNO POR OPERACION, porque cada una necesita su propia red:
# ver `_en_savepoint`.
_SP_DDL = "sp_conexiones_ddl"
_SP_DUENOS = "sp_conexiones_duenos"

# La tabla NO se llama `operator_status_history` a proposito. `operator_status.activo` es
# la baja LOGICA del tablero (prender/apagar un operador para que no ensucie los cuadros)
# y `users.status` es la CONEXION (online/offline). Son dos cosas distintas y ese nombre
# las confundiria para siempre.
_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS conexiones_operador (
        id            bigserial   PRIMARY KEY,
        user_id       uuid        NOT NULL,
        account       text        NOT NULL,
        -- El nombre viaja en la fila y no por join: `users` se pisa, y una desconexion de
        -- hace tres meses tiene que seguir diciendo de quien fue aunque el operador ya no
        -- exista en el CRM.
        operator_name text,
        -- Sin `status_ant` la fila no distingue una desconexion de una reconexion.
        status_ant    text,
        status_nuevo  text        NOT NULL,
        -- LOS DOS RELOJES. `last_seen` es cuando lo vio WHATICKET; `detected_at` es cuando
        -- lo vimos NOSOTROS. Su diferencia ES la latencia del sync del ETL, que hoy no se
        -- puede medir de ninguna otra forma: `users` se reescribe entero y `sync_state` no
        -- tiene clave para `lookups`. Guardar uno solo vuelve la pregunta imposible.
        last_seen     timestamptz,
        detected_at   timestamptz NOT NULL DEFAULT now()
    )""",
    # La consulta que viene es "desconexiones de UNA cuenta, lo mas reciente primero".
    # Parcial sobre offline, mismo idioma que `idx_operator_status_off`.
    "CREATE INDEX IF NOT EXISTS idx_conexiones_operador_off "
    "ON conexiones_operador (account, detected_at DESC) "
    "WHERE status_nuevo = 'offline'",
    # EL REGISTRADOR NO PUEDE TUMBAR A QUIEN LO LLAMA. Es la regla 1 de `errores.registrar`
    # y la de `worker._registrar_fallo`, y aca pesa mas: esto corre DENTRO de la transaccion
    # del ETL. Si alguien borra `conexiones_operador`, sin este bloque cada UPDATE de
    # `users` empezaria a fallar y el ETL dejaria de ingestar por culpa de una tabla de
    # auditoria. Se degrada a no registrar, que es lo correcto.
    #
    # WARNING Y NO SILENCIO: un WARNING no aborta la transaccion pero queda en el log de
    # Postgres. Perder eventos sin dejar rastro es el modo de falla que ya costo caro dos
    # veces en este proyecto (la alerta de espera que disparo 0 de 207, el PUT de
    # `operator_status` que devolvia 200 y mentia).
    # SECURITY DEFINER, Y NO ES OPCIONAL. El default de `CREATE FUNCTION` es SECURITY
    # INVOKER: el cuerpo corre con los privilegios de QUIEN dispara el trigger --el rol del
    # ETL-- y no del dueño de la funcion. Si ese rol no tiene INSERT en
    # `conexiones_operador` (ni USAGE en `conexiones_operador_id_seq`, que nace sin ACL), el
    # INSERT da permission denied, lo atrapa el EXCEPTION de abajo, y el historial queda
    # VACIO PARA SIEMPRE sin un solo error visible.
    #
    # REPRODUCIDO el 2026-09-03 en `whaticket_copia` con un rol que solo tenia SELECT+UPDATE
    # sobre `users`: 0 filas capturadas, y `count(*) FROM pg_trigger` devolvia 1. El
    # chequeo post-deploy daba VERDE mientras la auditoria no registraba nada. Es el mismo
    # modo de falla que la alerta de espera (0 de 207) y el PUT de `operator_status` que
    # devolvia 200 y mentia.
    #
    # POR QUE DEFINER Y NO UN GRANT AL ROL DEL ETL: el GRANT es config en otra base, en otro
    # repo, que hay que acordarse de aplicar en cada ambiente nuevo -- y su ausencia es
    # invisible. DEFINER deja la garantia dentro del objeto.
    #
    # `SET search_path` ES OBLIGATORIO CON DEFINER: sin el, quien pueda crear un objeto en un
    # schema que venga antes en el path del invocador secuestra la resolucion de nombres y
    # corre codigo con los privilegios del dueño. `pg_temp` va ULTIMO para que una tabla
    # temporaria no pueda sombrear a `conexiones_operador`.
    #
    """
    CREATE OR REPLACE FUNCTION registrar_conexion_operador() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp
    AS $fn$
    BEGIN
        BEGIN
            INSERT INTO conexiones_operador
                  (user_id, account, operator_name, status_ant, status_nuevo, last_seen)
            VALUES (NEW.id, NEW.account, NEW.name, OLD.status, NEW.status, NEW.last_seen);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'conexiones_operador: cambio de status NO registrado';
        END;
        RETURN NEW;
    END;
    $fn$""",
    # `CREATE OR REPLACE TRIGGER` (PG 14+, aca corre 16.14) en vez de DROP + CREATE: el
    # DROP deja una ventana --corta, pero real-- en la que un UPDATE del ETL no se registra.
    #
    # EL `WHEN` NO ES UNA OPTIMIZACION, ES LA CORRECTITUD DEL DATO. El upsert del ETL toca
    # las 49 filas en cada sync y `AFTER UPDATE OF status` dispara aunque el valor sea
    # identico: sin el filtro serian 49 filas de ruido por corrida en lugar de solo las
    # transiciones reales, y el historial nace inservible sin dar ningun error.
    """
    CREATE OR REPLACE TRIGGER trg_conexiones_operador
    AFTER UPDATE OF status ON users
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION registrar_conexion_operador()""",
    # EXECUTE ES PUBLIC POR DEFECTO, y con SECURITY DEFINER eso alcanza para ENVENENAR el
    # log de auditoria. No es ejecucion arbitraria --una funcion `RETURNS trigger` no se
    # puede invocar directo-- pero cualquier rol dueño de una tabla con columnas compatibles
    # puede colgar ESTA funcion como trigger de SU tabla y escribir filas con los privilegios
    # del dueño.
    #
    # REPRODUCIDO el 2026-09-03 con un rol no-superusuario:
    #     colgo la funcion DEFINER de SU tabla: SI
    #     fila envenenada: ('FALSA', 'Operador Inventado', 'online', 'offline')
    #
    # `REVOKE` es re-ejecutable: revocar algo ya revocado es un no-op, no un error. El dueño
    # conserva EXECUTE siempre, asi que el `CREATE TRIGGER` de `ensure_table` sigue andando,
    # y Postgres NO re-chequea EXECUTE cuando el trigger dispara (verificado: el rol del ETL
    # sin EXECUTE igual captura).
    "REVOKE EXECUTE ON FUNCTION registrar_conexion_operador() FROM PUBLIC",
)

# LA INVARIANTE DE DUEÑOS. SECURITY DEFINER solo alcanza si el dueño de la funcion puede
# escribir la tabla. Y `CREATE TABLE IF NOT EXISTS` es un NO-OP si la tabla ya existe, sin
# importar quien la creo: si la creo otro rol, la funcion corre como su propio dueño, el
# INSERT vuelve a dar permission denied, y `prosecdef` sigue diciendo `true` -- asi que el
# chequeo de SECURITY DEFINER NO lo detecta.
#
# LA SECUENCIA SE COMPARA IGUAL, pero es defensa en profundidad y no el caso comun: mientras
# este LIGADA a la tabla, Postgres se niega a cambiarle el dueño
# (`FeatureNotSupported: Sequence is linked to table`, verificado el 2026-09-03), asi que
# sigue al de la tabla sola. Solo se puede desalinear con un `ALTER SEQUENCE ... OWNED BY
# NONE` deliberado primero -- probado, y ahi el chequeo lo detecta.
#
# TODO POR OID, NUNCA POR NOMBRE. La primera version usaba subconsultas
# `WHERE proname = ...` / `WHERE relname = ...` sin calificar por schema, y con un homonimo
# en otro schema eso levanta -- reproducido el 2026-09-03:
#     CardinalityViolation: more than one row returned by a subquery used as an expression
# y encima deja la transaccion en InFailedSqlTransaction. `to_regclass` devuelve NULL en vez
# de error y no puede dar multifila; `WHERE oid = ...` acota a una fila por construccion; y
# `(tgrelid, tgname)` es unico en `pg_trigger`.
#
# EL OID DE LA FUNCION SALE DE `tgfoid`, que es la que el trigger VA A LLAMAR de verdad, y no
# la que resuelva el search_path del momento. Preguntar por el nombre contestaria sobre otra
# funcion que la que corre.
#
# DEVUELVE NULL, O NINGUNA FILA, CUANDO NO SE PUEDE SABER: `to_regclass` de algo ausente da
# NULL, y si el trigger no existe no hay fila. Quien lea esto tiene que tratar eso como
# "no se", NO como "bien" (ver `_duenos_coinciden`).
_DUENOS_SQL = """
SELECT (SELECT proowner FROM pg_proc  WHERE oid = t.tgfoid)
     = (SELECT relowner FROM pg_class WHERE oid = to_regclass('conexiones_operador'))
   AND (SELECT proowner FROM pg_proc  WHERE oid = t.tgfoid)
     = (SELECT relowner FROM pg_class WHERE oid = to_regclass('conexiones_operador_id_seq'))
  FROM pg_trigger t
 WHERE t.tgrelid = to_regclass('users')
   AND t.tgname  = 'trg_conexiones_operador'
   AND NOT t.tgisinternal
"""


def ensure_table(cur) -> None:
    """Crea `conexiones_operador`, su indice y el trigger sobre `users` (idempotente).

    Las cinco sentencias se pueden volver a correr (`IF NOT EXISTS`, `OR REPLACE` y un
    `REVOKE`, que es un no-op si ya estaba revocado): corre en cada ciclo del
    worker sin efecto, y auto-sana si un redeploy del ETL recrea `users` y se lleva el
    trigger puesto.
    """
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)


def _a_la_bitacora(exc: BaseException | None, context: dict) -> None:
    """Deja el fallo en la tabla `errors`, no solo en el log. NUNCA levanta.

    A LA TABLA Y NO SOLO A STDOUT: es el mismo argumento que ya esta escrito en el loop de
    alertas de `worker.py` -- stdout de un contenedor se pierde en el redeploy y `errors`
    sobrevive. Y acá pesa doble porque `errors` LA COMPARTE EL ETL, que es justamente quien
    puede otorgar el privilegio o alinear el dueño que falta.

    `arranque` es el componente del vocabulario acordado para migraciones y config, y va SIN
    `account` porque nada de esto es de una cuenta puntual (regla 6). `errores._escribir`
    abre su PROPIA conexion, asi que esto es seguro incluso con la transaccion del llamador
    recien abortada, y su limitador (5 por ventana de 60 s) evita que un fallo que se repite
    en cada ciclo inunde la tabla.

    El try NO es de mas: `errores.registrar` promete no levantar, pero esto no puede depender
    de esa promesa -- si algun dia rompe, se lleva puesto el manejo del fallo original y con
    el las alertas. Misma cautela que `worker._registrar_fallo` del lado del llamador.
    """
    try:
        from src import errores

        errores.registrar("arranque", exc, context=context)
    except Exception:  # noqa: BLE001 - una bitacora rota no puede abortar la guarda
        pass


def _en_savepoint(cur, nombre: str, hacer):
    """Corre `hacer()` dentro de un SAVEPOINT propio. Devuelve `(ok, valor_o_excepcion)`.

    ES EL UNICO LUGAR DEL MODULO QUE TOCA SAVEPOINTS, y eso es la leccion, no una
    preferencia de estilo. La version anterior tenia la danza escrita inline dentro de
    `asegurar_sin_romper`, y al agregar el chequeo de dueños quedo DESPUES del `RELEASE` y
    sin `try`: una sentencia sin red en la funcion cuyo contrato es no poder romper al
    llamador. Si levantaba, subia hasta el `except` ancho de `alertas.barrer` y las alertas
    VIP se apagaban en silencio. Con la danza en un solo lugar, toda operacion nueva entra
    por aca o no entra.

    UN SAVEPOINT POR OPERACION, no uno para todas: una sentencia que falla ABORTA la
    transaccion, asi que un `try/except` en Python no alcanza -- lo que siga muere con
    InFailedSqlTransaction igual. Y el savepoint del DDL ya esta liberado cuando corre el
    chequeo, asi que no puede cubrirlo.

    SIN TRANSACCION NO HAY SAVEPOINT: con `autocommit=True` pedirlo levanta
    NoActiveSqlTransaction. Ahi se corre sin red, que es correcto, porque cada sentencia es
    su propia transaccion y una que falla no arrastra al resto.

    NUNCA LEVANTA. Ni al tomar el savepoint, ni al cerrarlo, ni en el camino de error --que
    es justo donde mas importa no romper al llamador.
    """
    protegido = True
    try:
        cur.execute(f"SAVEPOINT {nombre}")
    except Exception:  # noqa: BLE001 - sin transaccion se sigue, sin red de contencion
        protegido = False

    def _cerrar(sentencia: str) -> None:
        if not protegido:
            return
        try:
            cur.execute(f"{sentencia} {nombre}")
        except Exception:  # noqa: BLE001 - cerrar la red no puede ser lo que rompa
            pass

    try:
        valor = hacer()
    except Exception as e:  # noqa: BLE001 - el llamador no se entera por una excepcion
        _cerrar("ROLLBACK TO SAVEPOINT")
        return False, e
    _cerrar("RELEASE SAVEPOINT")
    return True, valor


def _duenos_coinciden(cur) -> bool | None:
    """`True` / `False` / `None` cuando NO SE PUDO DETERMINAR. Ver `_DUENOS_SQL`.

    `None` es un valor de primera clase y no un caso raro: falta el trigger, falta la tabla,
    falta la secuencia. La version anterior comparaba `fila[0] is False` y con eso `None`
    caia al camino verde -- `None is False` es False. En un modulo cuyo punto entero es que
    el verde signifique algo, "no se" no puede leerse como "bien".
    """
    cur.execute(_DUENOS_SQL)
    fila = cur.fetchone()
    return None if fila is None else fila[0]


def asegurar_sin_romper(cur, log=None) -> bool:
    """`ensure_table` que NO puede tumbar a quien lo llama. True si la captura QUEDO VIVA.

    No alcanza con que el DDL corra: devuelve True solo si tambien se pudo VERIFICAR que la
    funcion y la tabla comparten dueño, porque sin eso el trigger existe y no inserta.

    QUE PUEDE FALLAR EN PRODUCCION, y por que no se ve en la copia. `CREATE TRIGGER` sobre
    `users` pide el privilegio TRIGGER y `CREATE FUNCTION` pide CREATE en el schema. El dueño
    de `users` es el ETL: si el dashboard conecta con un rol que no los tiene, esto revienta
    con permission denied. En `whaticket_copia` el rol es `whaticket`, superusuario Y dueño de
    la tabla, asi que ahi pasa siempre -- medido el 2026-09-03. La copia NO es evidencia de
    que en prod vaya a andar.

    DEGRADA A "SIN CAPTURA", NUNCA A "SIN ALERTAS". `alertas.barrer` tiene UN solo `try` que
    arranca en `ensure_table` y un `except` que devuelve todo en ceros: sin esta guarda, una
    tabla de auditoria nueva apagaria EN SILENCIO las alertas VIP que hoy funcionan en
    produccion, y el resultado se veria igual que un dia tranquilo.

    Y LO DICE, en el log y en la tabla `errors`. Degradar callado ya costo caro dos veces
    aca; ver `_a_la_bitacora`.
    """
    decir = log or (lambda m: logger.warning("%s", m))

    ok, res = _en_savepoint(cur, _SP_DDL, lambda: ensure_table(cur))
    if not ok:
        decir(f"[desconexiones] captura NO aplicada, las alertas siguen "
              f"({type(res).__name__}: {res}). Falta el privilegio TRIGGER sobre `users`?")
        _a_la_bitacora(res, {
            "que": "desconexiones.ensure_table",
            "efecto": "sin captura de conexion/desconexion; las alertas siguen",
            "revisar": "has_table_privilege(current_user, 'users', 'TRIGGER')",
        })
        return False

    # EL DDL CORRIO SIN ERROR Y LA CAPTURA PUEDE ESTAR IGUAL MUERTA. Ver `_DUENOS_SQL`: con
    # la tabla creada por otro rol, el INSERT del trigger da permission denied, lo traga el
    # EXCEPTION del plpgsql, y todos los chequeos de arriba dan verde. Se verifica en vez de
    # suponerse -- y con su PROPIO savepoint, porque el del DDL ya se libero.
    ok, res = _en_savepoint(cur, _SP_DUENOS, lambda: _duenos_coinciden(cur))
    if not ok:
        decir(f"[desconexiones] no se pudo verificar la invariante de dueños "
              f"({type(res).__name__}: {res}); la captura queda SIN confirmar")
        _a_la_bitacora(res, {
            "que": "chequeo de la invariante de dueños de desconexiones",
            "efecto": "no se sabe si el trigger puede insertar; las alertas siguen",
            "revisar": "src/desconexiones.py::_DUENOS_SQL",
        })
        return False
    if res is not True:
        motivo = ("la funcion y `conexiones_operador` tienen DUEÑOS DISTINTOS"
                  if res is False else
                  "faltan objetos (trigger, tabla o secuencia): no se pudo determinar")
        decir(f"[desconexiones] {motivo}: el trigger no va a poder insertar. "
              "Alinear con ALTER TABLE ... OWNER TO.")
        _a_la_bitacora(None, {
            "que": "invariante de dueños de desconexiones",
            "motivo": motivo,
            "efecto": "el trigger existe y NO captura; el historial queda vacio",
            "revisar": "pg_proc.proowner vs pg_class.relowner de conexiones_operador",
        })
        return False
    return True
