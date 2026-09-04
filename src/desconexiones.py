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
    # EL GEMELO PARA CONEXIONES. El indice de arriba es parcial sobre `offline` y no sirve
    # para "conexiones de UNA cuenta, lo mas reciente primero" -- la consulta de
    # `pendientes_conexion` filtra `online`. NO SE TOCA EL DE ARRIBA: ya esta desplegado, y
    # un `DROP` + recreate dejaria una ventana sin indice. `IF NOT EXISTS` lo hace re-ejecutable
    # igual que el resto de este modulo.
    "CREATE INDEX IF NOT EXISTS idx_conexiones_operador_on "
    "ON conexiones_operador (account, detected_at DESC) "
    "WHERE status_nuevo = 'online'",
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


# =====================================================================
# LA ALERTA: el drenaje del outbox
# =====================================================================
#
# EL TRIGGER ESCRIBE, ESTO MANDA. Ver el encabezado del modulo para por que el envio no
# vive en el trigger. Aca solo se elige QUE fila del historial merece un aviso; el envio,
# la idempotencia y los reintentos son los de `src/alertas.py`, ya probados en produccion.

# LA VENTANA, y es la leccion del apagon del 2026-09-03. El worker estuvo 93 minutos caido
# (apagon; antes ese mismo dia, el servidor del modelo). Cuando un worker vuelve, sin
# ventana dispara TODAS las desconexiones acumuladas de un saque. Y el sembrado de
# `alertas.ledger_vacio` NO cubre esto: solo protege el PRIMER arranque de la historia, no
# cada reinicio. Media hora es el corte: mas viejo que eso ya no es un aviso, es un informe.
VENTANA_ALERTA_MINUTOS = 30

# LA SEGUNDA RED. Un pico --un reinicio del CRM que desloguea a todos, un corte de red-- no
# puede volcar cien mensajes seguidos en el grupo. Lo que sobra del tope NO se pierde: queda
# sin marcar y entra en el ciclo siguiente, sesenta segundos despues.
#
# CINCO Y NO DIEZ, PORQUE EL CHAT ES COMPARTIDO con las alertas VIP (decision del negocio,
# 2026-09-03). El tope es POR CUENTA y hay dos, asi que diez serian veinte mensajes por ciclo
# de 60 s -- justo en el limite de Telegram para un grupo (~20/min) y sin dejar lugar a los
# resumenes VIP, que van al mismo lado. Un 429 no pierde el aviso (`REINTENTAR` lo desmarca y
# vuelve al ciclo siguiente), pero llenar la cuota con desconexiones RETRASA los resumenes,
# que son los que alguien puede querer accionar el mismo dia.
TOPE_POR_CICLO = 5

# NO SE AVISA DE LOS OPERADORES APAGADOS. `operator_status.activo = false` es la baja logica
# del tablero: esa persona no esta trabajando y su desconexion no le importa a nadie.
#
# EL MATCH VA POR CLAVE, NO POR STRING EXACTO. Es el bug que ya se pago con `operator_status`
# el 2026-08-27: la tabla tenia 'RAMIREZ', el modal mandaba 'Ramirez', el `ON CONFLICT` no
# matcheaba y el operador no se prendia NUNCA -- con 222 sesiones recientes. `clave_sql` es
# la fuente unica de esa regla (minusculas, sin tildes, con la ñ arreglada).
_PENDIENTES_SQL_TPL = """
SELECT c.id, c.account, c.operator_name, c.last_seen, c.detected_at,
       -- LA SESION QUE SE CIERRA CON ESTE `offline`, SI HAY UNA. Regla del negocio,
       -- decidida por el usuario y sin margen para interpretarla: solo CICLO CERRADO -- el
       -- `online` INMEDIATO ANTERIOR del MISMO (user_id, account). Si ese anterior no fue
       -- un `online` (otro `offline`, o no hay anterior) el CASE da NULL y
       -- `mensaje_desconexion` simplemente no agrega la linea -- ni "desconocido", ni una
       -- estimacion desde el arranque de la captura. Medido el 2026-09-03: 11 de 22
       -- operadores arrancan su historial con un `offline` porque ya estaban conectados
       -- cuando el trigger empezo a grabar (17:03 UTC); esos NO tienen sesion que contar.
       --
       -- EL EMPAREJADO VA POR `lag()`, no por un JOIN aparte ni por un loop en Python:
       -- particionado por (user_id, account) y ordenado por `detected_at` (NOT NULL, crece
       -- con cada INSERT del trigger) -- mismo patron que `_SESIONES_SQL`. La particion es
       -- lo que impide que el `online` anterior de OTRO operador o de OTRA cuenta se cuele.
       CASE WHEN c.status_evento_anterior = 'online'
            THEN c.sesion_last_seen_inicio END   AS sesion_last_seen_inicio,
       CASE WHEN c.status_evento_anterior = 'online'
            THEN c.sesion_detected_at_inicio END AS sesion_detected_at_inicio
  FROM (
        SELECT id, account, operator_name, status_nuevo, last_seen, detected_at,
               lag(status_nuevo) OVER w AS status_evento_anterior,
               lag(last_seen)    OVER w AS sesion_last_seen_inicio,
               lag(detected_at)  OVER w AS sesion_detected_at_inicio
          FROM conexiones_operador
        WINDOW w AS (PARTITION BY user_id, account ORDER BY detected_at)
       ) c
{guardas}"""


# EL BLOQUE COMPARTIDO ENTRE `pendientes` (offline) Y `pendientes_conexion` (online): el
# dedup contra `alertas_enviadas`, la ventana, el tope y la exclusion de operadores apagados
# por clave (`identidad.clave_sql`) son el MISMO texto en las dos consultas, salvo el `tipo`
# de alerta y el `estado` que filtran. Factorizado para que una regla nueva ahi -- otro
# guard, otro tope -- no se pueda actualizar en una consulta y olvidar en la otra.
#
# LO QUE NO ENTRA ACA: el SELECT y el FROM de verdad difieren. La de desconexion pairea la
# sesion que se cierra con `lag()` sobre una subconsulta con window function; la de conexion
# no tiene sesion que parear -- es un evento suelto. Forzar esa parte a un template comun le
# restaria mas legibilidad de la que ahorraria, asi que cada consulta se queda con su propio
# SELECT/FROM y comparten solo este bloque.
def _bloque_guardas_pendientes(tipo: str, estado: str) -> str:
    from src.identidad import clave_sql

    clave_os = clave_sql("os.operator_name")
    clave_c = clave_sql("c.operator_name")
    return f"""  LEFT JOIN alertas_enviadas a
         ON a.account = c.account
        AND a.tipo    = '{tipo}'
        AND a.clave   = c.id::text
 WHERE c.account = %(account)s
   AND c.status_nuevo = '{estado}'
   AND a.clave IS NULL
   AND c.detected_at > %(desde)s
   AND NOT EXISTS (
         SELECT 1 FROM operator_status os
          WHERE os.account = c.account
            AND os.activo = false
            AND {clave_os} = {clave_c})
 ORDER BY c.detected_at
 LIMIT %(tope)s
"""


def _pendientes_sql() -> str:
    return _PENDIENTES_SQL_TPL.format(
        guardas=_bloque_guardas_pendientes("desconexion", "offline"))


_PENDIENTES_SQL = _pendientes_sql()


def pendientes(cur, account: str, ahora, ventana_minutos: int = VENTANA_ALERTA_MINUTOS,
               tope: int = TOPE_POR_CICLO) -> list[dict]:
    """Las desconexiones de una cuenta que todavia no se avisaron. Mas viejas primero.

    `ahora` viene de la BASE (`alertas.ahora_de_la_base`), no de la app: es el mismo reloj
    con el que se escribio `detected_at`.
    """
    from datetime import timedelta

    cur.execute(_PENDIENTES_SQL, {
        "account": account,
        "desde": ahora - timedelta(minutes=ventana_minutos),
        "tope": tope,
    })
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, f)) for f in cur.fetchall()]


# LA CONSULTA GEMELA, para el otro lado del mismo ciclo. Mismas guardas que `_PENDIENTES_SQL`
# (ver `_bloque_guardas_pendientes`): dedup por evento, ventana, tope, exclusion de operadores
# apagados. NO HACE FALTA `lag()`/`lead()`: la conexion no tiene una sesion que parear, es un
# evento suelto -- por eso el SELECT/FROM es mas chico que el de desconexion.
_PENDIENTES_CONEXION_SQL_TPL = """
SELECT c.id, c.account, c.operator_name, c.last_seen, c.detected_at
  FROM conexiones_operador c
{guardas}"""


def _pendientes_conexion_sql() -> str:
    return _PENDIENTES_CONEXION_SQL_TPL.format(
        guardas=_bloque_guardas_pendientes("conexion", "online"))


_PENDIENTES_CONEXION_SQL = _pendientes_conexion_sql()


def pendientes_conexion(cur, account: str, ahora, ventana_minutos: int = VENTANA_ALERTA_MINUTOS,
                        tope: int = TOPE_POR_CICLO) -> list[dict]:
    """Las conexiones de una cuenta que todavia no se avisaron. Mas viejas primero.

    MISMO CONTRATO que `pendientes`: `ahora` viene de la BASE (`alertas.ahora_de_la_base`), no
    de la app, porque es el mismo reloj con el que se escribio `detected_at`.
    """
    from datetime import timedelta

    cur.execute(_PENDIENTES_CONEXION_SQL, {
        "account": account,
        "desde": ahora - timedelta(minutes=ventana_minutos),
        "tope": tope,
    })
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, f)) for f in cur.fetchall()]


def clave_alerta(evento_id) -> str:
    """LA CLAVE ES EL EVENTO, no el operador.

    Un operador se desconecta muchas veces: dedupear por operador dejaria muda la segunda
    desconexion y todas las que siguen. Es el mismo razonamiento por el que
    `alertas.clave_resumen` paso de sesion a interaccion.

    LA MISMA FUNCION SIRVE PARA CONEXION Y DESCONEXION: la regla ("el evento, no el
    operador") no depende de que transicion sea, y `_bloque_guardas_pendientes` ya
    distingue el `tipo` ('conexion' / 'desconexion') en el dedup contra `alertas_enviadas`.
    No hace falta una `clave_alerta_conexion` aparte.
    """
    return str(evento_id)


def canal_desde_env(env):
    """El canal del aviso de conexion/desconexion: EL MISMO GRUPO que las alertas VIP.

    DECISION DEL NEGOCIO (2026-09-03): un solo grupo. El aviso tiene otro proposito, no otro
    destino. Por eso el canal sale de `alertas.canal_desde_env` y NO tiene variables propias:
    dos pares de variables apuntando al mismo chat se desincronizan el dia que se rote el
    token, y nadie se entera hasta que una de las dos alertas deja de llegar.

    EL CANAL SE COMPARTE, EL INTERRUPTOR NO. `ALERTA_DESCONEXION` (vacio = apagado) es
    aparte, y no es celo: el canal VIP YA esta configurado en produccion, asi que sin flag
    esta alerta se prenderia sola en el momento del deploy -- con un volumen estimado de 50 a
    150 avisos por dia (49 operadores por una o tres desconexiones) que todavia NO se midio.
    Y un grupo que se llena de avisos que no importan se deja de leer: la alerta de espera
    disparo 0 de 207 y se dio de baja.

    El historial se llena IGUAL con la alerta apagada, asi que el volumen se mide sobre datos
    propios y despues se prende. Mismo patron opt-in que `SCORING_ENABLED` y `API_DOCS`: el
    default tiene que ser el seguro, no el comodo. La consulta para medirlo esta en
    docs/auditoria-desconexiones.md.

    UN SOLO FLAG PARA LAS DOS MITADES DEL CICLO, y no `ALERTA_CONEXION` aparte. Conexion y
    desconexion son las dos mitades de UN ciclo (`online` -> `offline`); un interruptor
    separado dejaria prender solo una mitad, que es peor que las dos apagadas -- un aviso de
    "se conecto" sin su contraparte "se desconecto" (o viceversa) confunde mas de lo que
    informa. Si, el nombre `ALERTA_DESCONEXION` quedo mas angosto de lo que ahora significa;
    renombrarlo rompe el deploy que ya lo tiene seteado en produccion, y ese costo no lo paga
    una mejora cosmetica de nombre.
    """
    from src.alertas import Canal, canal_desde_env as canal_vip
    # `config._bool` y no una lista propia de valores verdaderos: tener dos definiciones de
    # "booleano de entorno" es como se desincronizan: si alguien agrega un valor alla, esta
    # copia no lo sigue. Es el mismo argumento de la zona horaria unas lineas mas abajo.
    from src.config import _bool

    if not _bool(env.get("ALERTA_DESCONEXION")):
        return Canal("", "")
    return canal_vip(env)


# =====================================================================
# LAS SESIONES DE CONEXION: el ciclo cerrado online -> offline
# =====================================================================
#
# LA REGLA DEL NEGOCIO, decidida por el usuario y sin margen para interpretarla: una sesion
# es un `online` emparejado con el SIGUIENTE `offline` del mismo (user_id, account). Si un
# `online` todavia no tiene su `offline` -- el operador sigue conectado -- esa sesion NO
# EXISTE. No se devuelve, no se estima "tiempo hasta ahora". "Debe ser un ciclo cerrado, abre
# y cierra, se calcula el tiempo entre ellos, no hay pierde, no podemos hacer mas."
#
# EL EMPAREJADO VA POR `lead()`, particionado por `(user_id, account)` y ordenado por
# `detected_at` -- que es NOT NULL y crece con cada INSERT del trigger, asi que ordena la
# secuencia real de eventos aunque `last_seen` este ausente en alguna fila. Reimplementar el
# emparejado con un loop en Python duplicaria algo que el motor ya resuelve en una sola
# pasada, y de forma correcta por construccion.
_SESIONES_SQL = """
SELECT user_id, account, operator_name,
       last_seen       AS last_seen_inicio,
       detected_at     AS detected_at_inicio,
       last_seen_sig   AS last_seen_fin,
       detected_at_sig AS detected_at_fin
  FROM (
        SELECT user_id, account, operator_name, status_nuevo,
               last_seen, detected_at,
               lead(status_nuevo) OVER w AS status_sig,
               lead(last_seen)    OVER w AS last_seen_sig,
               lead(detected_at)  OVER w AS detected_at_sig
          FROM conexiones_operador
        WINDOW w AS (PARTITION BY user_id, account ORDER BY detected_at)
       ) t
 WHERE status_nuevo = 'online'
   AND status_sig = 'offline'
 ORDER BY account, user_id, detected_at
"""


def sesiones_cerradas(cur) -> list[dict]:
    """Las sesiones de conexion CERRADAS: un `online` emparejado con el SIGUIENTE `offline`
    del mismo (user_id, account). El operador todavia conectado NO aparece -- ver el
    encabezado de esta seccion.

    LA DURACION SALE DE `last_seen`, NO DE `detected_at`. `detected_at` es cuando el SYNC se
    dio cuenta, no cuando paso: medido el 2026-09-03 sobre 58 filas, la latencia mediana es
    157 s en offline y 151 s en online, con un maximo de ~5 minutos. En una sesion corta real
    la diferencia fue de 5 minutos por `detected_at` contra 9 minutos por `last_seen` -- un
    error del 80%, justo en las sesiones cortas que son las que le importan al negocio.

    `last_seen` es NULLABLE en el esquema (hoy lleno en 58/58 filas de la copia, pero la
    columna lo permite): cuando falta, esta funcion cae a `detected_at` para esa punta.

    Cada elemento: `account`, `user_id`, `operator_name`, `start`, `end`,
    `duration_seconds`.
    """
    cur.execute(_SESIONES_SQL)
    cols = [d.name for d in cur.description]
    filas = [dict(zip(cols, f)) for f in cur.fetchall()]

    sesiones = []
    for f in filas:
        inicio = f["last_seen_inicio"] if f["last_seen_inicio"] is not None \
            else f["detected_at_inicio"]
        fin = f["last_seen_fin"] if f["last_seen_fin"] is not None \
            else f["detected_at_fin"]
        sesiones.append({
            "account": f["account"],
            "user_id": f["user_id"],
            "operator_name": f["operator_name"],
            "start": inicio,
            "end": fin,
            "duration_seconds": int((fin - inicio).total_seconds()),
        })
    return sesiones


def mensaje_desconexion(d: dict) -> str:
    """El aviso, en HTML.

    EN HTML Y ESCAPADO: los nombres vienen del CRM y un `_` o un `&` ya rompieron un envio
    real con `400 can't parse entities` (ver `alertas.Canal.enviar`).

    LA HORA VA EN ECUADOR. Quien lee el grupo no tiene que restar cinco horas a mano.

    LA LINEA DE SESION es opcional y depende de `sesion_last_seen_inicio` /
    `sesion_detected_at_inicio`, que `_PENDIENTES_SQL_TPL` deja en NULL cuando el `offline`
    no tuvo un `online` inmediato anterior en el mismo (user_id, account) -- regla del
    negocio, sin margen para interpretarla: SOLO CICLO CERRADO. Sin esas claves (o con
    ambas en NULL) el mensaje sale IDENTICO al de siempre.
    """
    from src.alertas import _esc, _quien
    # LA MISMA FUENTE QUE EL RESTO DE LAS ALERTAS: `src/horario.TZ`, que es la que importa
    # `alertas.py`. Ecuador no tiene horario de verano, asi que el offset fijo alcanza -- y
    # tener dos definiciones de "la hora local" es como se desincronizan los informes.
    from src.horario import TZ as TZ_EC

    def _hora(v) -> str:
        return v.astimezone(TZ_EC).strftime("%H:%M:%S") if v else "?"

    def _duracion(segundos: int) -> str:
        """Horas y minutos en español, minutos solos bajo la hora -- ej. "3 h 12 min" o
        "45 min". Sin segundos: a esta escala (sesiones de operador) no aportan nada."""
        minutos = segundos // 60
        horas, minutos = divmod(minutos, 60)
        return f"{horas} h {minutos} min" if horas else f"{minutos} min"

    cuerpo = [
        "🔌 <b>Operador desconectado</b>",
        "",
        f"🧑‍💼 <b>{_quien(d.get('operator_name'))}</b>  ·  cuenta {_esc(d.get('account'))}",
        f"🕐 {_hora(d.get('last_seen'))} (Ecuador)",
    ]

    # EL RELOJ ES `last_seen`, NO `detected_at` -- mismo argumento que `sesiones_cerradas`:
    # la latencia mediana del sync es 157 s en offline y 151 s en online (maximo ~5 min), y
    # en una sesion corta real eso fue un error del 80% (5 min por `detected_at` contra 9
    # min por `last_seen`). `detected_at` entra solo como fallback POR PUNTA, cuando
    # `last_seen` es NULL en esa punta -- la columna lo permite en el esquema.
    inicio = d.get("sesion_last_seen_inicio")
    if inicio is None:
        inicio = d.get("sesion_detected_at_inicio")
    if inicio is not None:
        fin = d.get("last_seen")
        if fin is None:
            fin = d.get("detected_at")
        duracion_seg = int((fin - inicio).total_seconds())
        cuerpo.append(f"🕓 tiempo de sesion {_duracion(duracion_seg)} (desde {_hora(inicio)})")

    # LA LATENCIA SE MUESTRA cuando es grande: un aviso de una desconexion de hace veinte
    # minutos tiene que decir que llego tarde, no dejar creer que acaba de pasar.
    visto, detectado = d.get("last_seen"), d.get("detected_at")
    if visto and detectado:
        seg = int((detectado - visto).total_seconds())
        if seg >= 60:
            cuerpo.append(f"⏱ detectado {seg // 60} min despues")
    return "\n".join(cuerpo)


def mensaje_conexion(d: dict) -> str:
    """El aviso de CONEXION, en HTML. Mismo escapado y misma hora Ecuador que
    `mensaje_desconexion` -- ver ese docstring para el porque (nombres del CRM, `400 can't
    parse entities`, hora local para no restar cinco horas a mano).

    SIN NINGUNA LINEA DE DURACION, A PROPOSITO. Al momento de conectar la sesion todavia no
    cerro -- no existe hasta que llegue el `offline` que la cierra (ver la seccion "LAS
    SESIONES DE CONEXION" mas abajo) -- asi que no hay nada que resumir. La duracion aparece
    recien en `mensaje_desconexion`, cuando el ciclo se cierra.

    LA LATENCIA SI APLICA IGUAL: es la misma metrica del sync (mediana 151 s en `online`, ver
    el encabezado del modulo), y decir "esto llego con retraso" vale tanto para una conexion
    como para una desconexion.
    """
    from src.alertas import _esc, _quien
    from src.horario import TZ as TZ_EC

    def _hora(v) -> str:
        return v.astimezone(TZ_EC).strftime("%H:%M:%S") if v else "?"

    cuerpo = [
        "🟢 <b>Operador conectado</b>",
        "",
        f"🧑‍💼 <b>{_quien(d.get('operator_name'))}</b>  ·  cuenta {_esc(d.get('account'))}",
        f"🕐 {_hora(d.get('last_seen'))} (Ecuador)",
    ]

    # LA LATENCIA, IGUAL QUE EN `mensaje_desconexion`: un aviso de una conexion de hace veinte
    # minutos tiene que decir que llego tarde.
    visto, detectado = d.get("last_seen"), d.get("detected_at")
    if visto and detectado:
        seg = int((detectado - visto).total_seconds())
        if seg >= 60:
            cuerpo.append(f"⏱ detectado {seg // 60} min despues")
    return "\n".join(cuerpo)


# =====================================================================
# LA SONDA DE ARRANQUE
# =====================================================================
#
# POR QUE EXISTE, y es la misma razon que `errores.estado`: el orden de deploy y los
# privilegios NO SE PUEDEN DEDUCIR DEL CODIGO. Sin esta linea, un trigger que quedo sin
# crear --o creado y sin poder insertar-- se ve EXACTAMENTE igual que un dia en que nadie se
# desconecto: las dos cosas son un historial vacio.
#
# LO QUE ESTA LINEA NO CUBRE: es de ARRANQUE, asi que no ve una captura que se muere estando
# el worker vivo. Eso lo cubre `asegurar_sin_romper`, que revisa la invariante de dueños en
# cada ciclo de 60 s y lo deja en `errors`. Entre las dos queda un hueco angosto: trigger
# presente, dueños alineados, y el INSERT fallando por otra causa. Ese se ve al proximo
# reinicio, en el conteo de eventos de esta misma linea.

_ESTADO_SQL = """
SELECT (SELECT count(*) FROM pg_trigger t
         WHERE t.tgrelid = to_regclass('users')
           AND t.tgname  = 'trg_conexiones_operador'
           AND NOT t.tgisinternal),
       (SELECT prosecdef FROM pg_proc
         WHERE oid = to_regprocedure('registrar_conexion_operador()')),
       CASE WHEN to_regclass('users') IS NULL THEN NULL
            ELSE has_table_privilege(current_user, 'users', 'TRIGGER') END,
       to_regclass('conexiones_operador') IS NOT NULL
"""


def estado(dsn: str, connect=None) -> str:
    """UNA linea para el log de arranque: la captura esta viva, o por que no.

    NUNCA LEVANTA. Es un log de arranque: no puede impedir que el worker levante. Mismo
    contrato que `errores.estado`.

    `connect` inyectable como en `ErrorLog`, para poder probarla sin base.

    TODO POR OID / `to_regclass`, igual que `_DUENOS_SQL`: un homonimo en otro schema no
    puede volverla multifila, y un objeto ausente da NULL en vez de reventar. Un `SELECT` y
    no un `INSERT` de prueba, por el mismo motivo que en `errores.estado`: verificar
    escribiendo dejaria una fila basura en cada arranque.
    """
    def _abrir():
        import psycopg

        return psycopg.connect(dsn, connect_timeout=8)

    try:
        with (connect or _abrir)() as conn:
            with conn.cursor() as cur:
                cur.execute(_ESTADO_SQL)
                trg, definer, puede_trigger, tabla = cur.fetchone()

                # SE SONDEA DESPUES DE ASEGURAR (ver `run_worker_loop`), asi que una tabla
                # ausente ACA no es "todavia no le toco": es que el DDL no pudo correr.
                #
                # LA PRIMERA VERSION DECIA "(la crea el worker en su primer ciclo)" y la
                # sonda corria ANTES del primer ciclo del hilo de alertas, asi que informaba
                # esto en CADA arranque -- visto en produccion el 2026-09-03 16:31:49. El
                # texto tranquilizador tapaba un bug de orden, y una linea que grita en falso
                # todos los dias es una linea que nadie va a leer el dia que sea verdad.
                if not tabla:
                    return ("captura de desconexiones NO ACTIVA: el DDL no pudo crear "
                            "`conexiones_operador`; revisar CREATE en el schema y el log "
                            "de `errors` (component=arranque)")
                if not trg:
                    porque = ("" if puede_trigger else
                              "; falta el privilegio TRIGGER sobre `users` "
                              "(lo otorga el dueño de la tabla, que es el ETL)")
                    return ("captura de desconexiones NO ACTIVA: sin trigger sobre "
                            f"`users`{porque}")
                # SECURITY INVOKER = el INSERT corre como el rol del ETL. Reproducido: 0
                # filas capturadas con el trigger presente y todo en verde.
                if not definer:
                    return ("captura de desconexiones ROTA: la funcion no es SECURITY "
                            "DEFINER, asi que el INSERT corre con los privilegios del ETL "
                            "y el historial se queda vacio en silencio")
                if _duenos_coinciden(cur) is not True:
                    return ("captura de desconexiones ROTA: la funcion y "
                            "`conexiones_operador` no comparten dueño, asi que el trigger "
                            "no puede insertar. Alinear con ALTER TABLE ... OWNER TO")

                cur.execute("SELECT count(*), now() - max(detected_at) "
                            "FROM conexiones_operador")
                n, hace = cur.fetchone()
                # CERO NO ES UN ERROR EN UN DESPLIEGUE NUEVO: nadie se desconecto todavia.
                # Decir "roto" aca entrena a la gente a ignorar la linea, que es como se
                # pierde la unica señal que teniamos.
                if not n:
                    return ("captura de desconexiones lista, 0 eventos todavia "
                            "(normal recien desplegado; si sigue en 0 al proximo "
                            "reinicio, revisar)")
                return f"captura de desconexiones lista ({n} eventos, el ultimo hace {hace})"
    except Exception as e:  # noqa: BLE001 - cualquier fallo se traduce a texto
        return ("no se pudo verificar la captura de desconexiones: "
                f"{type(e).__name__}: {str(e)[:200]}")
