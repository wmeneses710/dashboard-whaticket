# Auditoría de conexión/desconexión de operadores

Estado al 2026-09-03: **la captura y la alerta están construidas y verificadas. La alerta
está APAGADA por defecto** — a propósito, porque el volumen real todavía no se midió. Ver
"Cómo prender la alerta".

## El problema que resuelve

`users` es un *snapshot*. El ETL reescribe `status`, `last_seen` y `refreshed_at` en cada
pasada, y el estado anterior desaparece. A la pregunta *"¿quién se desconectó ayer a las
3pm?"* solo se puede responder por los operadores que **no volvieron a conectarse**; quien
se desconectó y regresó más tarde es invisible.

## Qué se construyó

| Objeto | Definido en |
|---|---|
| `conexiones_operador` (tabla) | `src/desconexiones.py` |
| `idx_conexiones_operador_off` (índice parcial) | `src/desconexiones.py` |
| `registrar_conexion_operador()` (función plpgsql, `SECURITY DEFINER`) | `src/desconexiones.py` |
| `trg_conexiones_operador` (trigger sobre `users`) | `src/desconexiones.py` |

El DDL lo asegura `alertas.ensure_table` vía `desconexiones.asegurar_sin_romper`, que corre
en cada ciclo del worker (60 s). Las cinco sentencias son re-ejecutables: `IF NOT EXISTS`,
`OR REPLACE` y un `REVOKE` (revocar algo ya revocado es un no-op).

### Por qué un trigger y no un poller

La alternativa era leer `users` cada 60 s y comparar contra la lectura anterior. Pierde
eventos: si el ETL escribe dos veces dentro de la misma ventana, la transición intermedia
no existió nunca para el dashboard. El trigger observa todos los cambios por construcción,
y es agnóstico del escritor.

### Por qué la alerta NO vive en el trigger

Un `sendMessage` desde el trigger correría **dentro de la transacción del ETL**:

1. Acopla el camino de escritura del ETL a la disponibilidad de Telegram.
2. Un envío no se puede deshacer: un *rollback* posterior dejaría una alerta por un evento
   que nunca se confirmó.
3. Postgres no habla HTTP — requeriría `pg_net`, `plpython3u` o la extensión `http`.
4. Descartaría lo ya probado en producción: `alertas_enviadas`, la clasificación
   `OK`/`REINTENTAR`/`DESCARTAR` y el escape de HTML.

Diseño elegido: patrón **outbox**. El trigger escribe la fila (transaccional, sin red) y el
hilo de alertas la drena con `Canal.enviar` + `marcar_enviada`. Cola de pendientes prevista
sin mecanismo nuevo: `alertas_enviadas` con `tipo='desconexion'` y `clave` =
`conexiones_operador.id`, resuelta con un `LEFT JOIN`.

## Los seis modos de falla silenciosa, y cómo se cubren

Todos comparten la misma forma: **el chequeo obvio da verde y la captura no funciona.**
Se encontraron de a uno, cada uno después de declarar el anterior "cubierto" — y el sexto
lo **abrió la guarda que cerró el quinto**. Cinco de los seis se reprodujeron contra base
real antes de arreglarlos.

### 1. En tiempo de deploy: el `CREATE TRIGGER` puede fallar por privilegios

`CREATE TRIGGER` sobre `users` requiere el privilegio `TRIGGER`, y `CREATE FUNCTION`
requiere `CREATE` en el schema. El dueño de `users` es el ETL.

`alertas.barrer` tiene un único `try` que arranca en `ensure_table` y un `except` que
devuelve todo en ceros. **Y en Postgres un DDL que falla aborta la transacción entera**, así
que atrapar la excepción en Python no alcanza: la sentencia siguiente muere con
`InFailedSqlTransaction`. Sin cubrirlo, esta tabla nueva habría apagado en silencio las
alertas VIP que hoy funcionan.

Cubierto por `desconexiones.asegurar_sin_romper`: corre el DDL en un `SAVEPOINT`, degrada a
*"sin captura"* y nunca a *"sin alertas"*, deja una línea de log y reintenta al ciclo
siguiente — así que otorgar el privilegio más tarde lo arregla sin redeploy.

### 2. En tiempo de ejecución: el `INSERT` corre como el rol del ETL

**Este es el que casi se escapa.** Por defecto `CREATE FUNCTION` es `SECURITY INVOKER`: el
cuerpo corre con los privilegios de **quien dispara el trigger** — el rol del ETL — no del
dueño de la función. Si ese rol no tiene `INSERT` en `conexiones_operador` (ni `USAGE` en
`conexiones_operador_id_seq`, que nace sin ACL), el `INSERT` da *permission denied*, lo
atrapa el `EXCEPTION WHEN OTHERS` y **el historial queda vacío para siempre sin un solo
error visible**.

Reproducido el 2026-09-03 en `whaticket_copia`, con un rol que solo tenía `SELECT` y
`UPDATE` sobre `users`:

```
UPDATE corriendo como: rol_prueba_etl
el UPDATE del ETL: NO fallo (el guard hizo su trabajo)
filas capturadas: 0
contar pg_trigger devuelve: 1   <- todo verde, y miente
```

La separación de roles no es hipotética en esa base:

```
conexiones_operador relacl = {whaticket=arwdDxt/whaticket, whaticketread=r/whaticket}
conexiones_operador_id_seq relacl = NULL      -- sin ACL: solo el dueño
roles con login            = whaticket (superuser), whaticketread
```

Cubierto con `SECURITY DEFINER` + `SET search_path = pg_catalog, public, pg_temp`. Mismo
experimento después del fix: **1 fila capturada**.

Se eligió `SECURITY DEFINER` y no un `GRANT` al rol del ETL porque el `GRANT` es
configuración en otra base, en otro repo, que hay que recordar aplicar en cada ambiente
nuevo — y su ausencia es invisible. `DEFINER` deja la garantía dentro del objeto.

`SET search_path` es obligatorio con `DEFINER`: sin él, quien pueda crear un objeto en un
schema que venga antes en el path del invocador secuestra la resolución de nombres y corre
código con los privilegios del dueño. `pg_temp` va último.

### 3. `EXECUTE` es PUBLIC: envenenamiento del log de auditoría

Una versión anterior de este documento afirmaba que *"no hay camino de escalada"*, porque
una función `RETURNS trigger` no se puede invocar directamente. **La premisa es cierta y la
conclusión era falsa.**

`EXECUTE` es `PUBLIC` por defecto (`proacl = NULL`). Con `SECURITY DEFINER`, eso alcanza
para que cualquier rol dueño de una tabla con columnas compatibles cuelgue esta función como
trigger de **su** tabla y escriba filas en `conexiones_operador` con los privilegios del
dueño. No es ejecución arbitraria — es **envenenamiento del log de auditoría**. Severidad
baja; la afirmación era más absoluta que el hecho.

Reproducido el 2026-09-03 con un rol no-superusuario:

```
el atacante tiene EXECUTE sobre la funcion: True
colgo la funcion DEFINER de SU tabla: SI
fila envenenada: ('FALSA', 'Operador Inventado', 'online', 'offline')
```

Cerrado con `REVOKE EXECUTE ON FUNCTION registrar_conexion_operador() FROM PUBLIC`. Después
del fix el intento muere en el `CREATE TRIGGER` con
`InsufficientPrivilege: permission denied for function`, y 0 filas falsas.

Dos cosas que hubo que verificar antes de aceptar el `REVOKE`:

- **Postgres no re-chequea `EXECUTE` cuando el trigger dispara.** Medido: el rol del ETL con
  `EXECUTE=False` capturó la fila igual. Si lo re-chequeara, el `REVOKE` habría matado la
  captura — el privilegio se valida al crear el trigger, no al ejecutarlo.
- **`REVOKE` es re-ejecutable**: revocar algo ya revocado es un no-op.

> El `REVOKE` se había rechazado antes con el argumento de que *"rompería la invariante de
> que toda sentencia del DDL sea idempotente"*. Esa invariante estaba escrita como un test de
> **string** (`"IF NOT EXISTS" in stmt or "OR REPLACE" in stmt`), no como la propiedad real
> (*se puede volver a correr*). El proxy bloqueó un arreglo de seguridad correcto. El test
> ahora expresa la propiedad y admite `REVOKE`.

### 4. La invariante de dueños

`SECURITY DEFINER` solo alcanza si el dueño de la función puede escribir la tabla. Y
**`CREATE TABLE IF NOT EXISTS` es un no-op si la tabla ya existe, sin importar quién la
creó**: con la tabla creada por otro rol, la función corre como su propio dueño, el `INSERT`
vuelve a dar *permission denied*, y `prosecdef` sigue diciendo `true` — así que el chequeo de
`SECURITY DEFINER` no lo detecta.

`asegurar_sin_romper` lo verifica en cada ciclo comparando `pg_proc.proowner` contra
`pg_class.relowner` de la tabla **y de la secuencia** (el `bigserial` la crea a nombre de
quien creó la tabla). Si no coinciden: devuelve `False`, lo dice en el log y lo registra en
`errors`. Verificado cambiando el dueño de la tabla: `True` → `False` → `True`.

Se corrige con `ALTER TABLE conexiones_operador OWNER TO <dueño de la función>`.

### 5. `SAVEPOINT` fuera de una transacción

`SAVEPOINT` solo existe dentro de un bloque de transacción: con `autocommit=True` levanta
`NoActiveSqlTransaction`, y `asegurar_sin_romper` — cuya razón de ser es no poder romper al
llamador — rompía al llamador.

No era un bug en producción (`alertas.barrer` usa `conn.commit()`, o sea sin autocommit),
pero era una mina para el próximo llamador, que habría caído en el `except` ancho de `barrer`
dejando las alertas en ceros. Lo destapó el propio script de verificación. Ahora si el
`SAVEPOINT` no se puede tomar, el DDL corre sin red — que es correcto, porque con autocommit
cada sentencia es su propia transacción y un DDL fallido no arrastra al resto.

### 6. El chequeo de dueños quedó fuera de la red

**La guarda que cerró el modo 5 abrió este.** El chequeo de la invariante de dueños quedó
*después* del `RELEASE SAVEPOINT` y sin `try`. Si esa sentencia levantaba, la excepción subía
a `alertas.ensure_table` → `alertas.barrer` → `except` ancho → alertas en ceros. Es el daño
exacto que el savepoint existe para evitar, en la función cuyo contrato declarado es no poder
tumbar a quien la llama.

Y había una vía concreta: las subconsultas de `_DUENOS_SQL` buscaban por `proname` / `relname`
sin calificar por schema. Reproducido el 2026-09-03 creando un homónimo en otro schema:

```
la consulta vieja LEVANTA: CardinalityViolation
  more than one row returned by a subquery used as an expression
y la transaccion queda ABORTADA: InFailedSqlTransaction
```

La transacción abortada es lo que cierra el caso: un `try/except` en Python **tampoco** habría
salvado a `barrer`. Cada operación necesita su propio savepoint.

Segundo defecto en las mismas cuatro líneas: `fila[0] is False` leía `NULL` como verde. Con un
objeto ausente la expresión da `NULL`, y `None is False` es `False`, así que caía al
`return True`. Reproducido: la consulta devolvía `(None,)` y el módulo reportaba verde. En un
módulo cuyo punto entero es que el verde signifique algo, *"no sé"* no puede leerse como
*"bien"*.

**Arreglado en tres frentes:**

- La danza del savepoint vive ahora en **un solo lugar** (`_en_savepoint`), con **un savepoint
  por operación**. Tenerla escrita *inline* es lo que permitió que una sentencia quedara
  afuera; ahora toda operación nueva entra por ahí o no entra. Verificado: una consulta que
  revienta adentro devuelve `ok=False` y la transacción sigue viva.
- `_DUENOS_SQL` pregunta **por OID, nunca por nombre**: `to_regclass` (devuelve `NULL` en vez
  de error, no puede dar multifila), `WHERE oid = ...` (una fila por construcción) y
  `pg_trigger` acotado por `(tgrelid, tgname)`, que es único. El OID de la función sale de
  `tgfoid` — la que el trigger *va a llamar de verdad*, no la que resuelva el `search_path`
  del momento. Verificado con el homónimo presente: no levanta, y la transacción sobrevive.
- `_duenos_coinciden` devuelve `True` / `False` / **`None`**, y solo `True` es verde.
  Verificado en vivo: sin el trigger devuelve `None`.

> **Matiz sobre la secuencia.** El chequeo compara también el dueño de
> `conexiones_operador_id_seq`, pero es defensa en profundidad y no el caso común: mientras
> esté **ligada** a la tabla, Postgres se niega a cambiarle el dueño
> (`FeatureNotSupported: Sequence is linked to table`). Solo se desalinea con un
> `ALTER SEQUENCE ... OWNED BY NONE` deliberado primero — probado, y ahí el chequeo lo
> detecta (`False`).

### A dónde va cada fallo

| Fallo | Dónde queda | Sobrevive al redeploy |
|---|---|---|
| `CREATE TRIGGER` / `CREATE FUNCTION` (deploy) | log del worker **+ tabla `errors`** (`source='dashboard'`, `component='arranque'`) | **sí** |
| `INSERT` del trigger (ejecución) | `RAISE WARNING` → log de Postgres | **no** |

El fallo de deploy va a `errors` por el mismo argumento que ya está escrito en el loop de
alertas de `worker.py`: *"stdout de un contenedor se pierde en el redeploy; `errors`
sobrevive (y la comparte el ETL)"*. Y la comparte el ETL importa acá, porque el ETL es quien
puede otorgar el privilegio que falta. El rate limit de `errores` (5 por ventana de 60 s)
evita que un fallo que se repite en cada ciclo inunde la tabla.

**Lo que NO está cubierto:** el `RAISE WARNING` del trigger solo llega al log de Postgres.
Y ese es justamente el peor, porque produce un historial vacío con todo en verde. No se
puede escribir en `errors` desde el `EXCEPTION` del plpgsql de forma confiable: iría dentro
de la transacción del ETL, y si esa transacción hace *rollback* el registro del error se va
con ella.

La cobertura es una **línea en el log de arranque**, `desconexiones.estado()`, igual que la
de `errores.estado` y la de las alertas VIP. Sin ella, un trigger que quedó sin crear — o
creado y sin poder insertar — se ve **exactamente igual** que un día en que nadie se
desconectó: las dos cosas son un historial vacío.

Se emite en `run_worker_loop`, al lado de `[worker] alertas VIP: on|off`, y distingue cinco
estados:

| Línea | Qué pasó |
|---|---|
| `lista (N eventos, el ultimo hace X)` | está capturando |
| `lista, 0 eventos todavia (normal recien desplegado...)` | recién subido, nadie se desconectó aún |
| `NO ACTIVA: falta la tabla ...` | el worker no completó su primer ciclo |
| `NO ACTIVA: sin trigger sobre users; falta el privilegio TRIGGER ...` | el modo de falla 1 |
| `ROTA: la funcion no es SECURITY DEFINER ...` / `... no comparten dueño ...` | los modos 2 y 4 |

`0 eventos` **no** se reporta como error recién desplegado: nadie se desconectó todavía, y
decir "roto" ahí entrena a la gente a ignorar la línea — que es como se pierde la única señal
que había. Lo que sí importa es `0 eventos` en un worker que lleva días arriba.

**Lo que esta línea no cubre:** es de arranque, así que no ve una captura que se muere estando
el worker vivo. Eso lo cubre `asegurar_sin_romper`, que revisa la invariante de dueños en cada
ciclo de 60 s y lo deja en `errors`. Entre las dos queda un hueco angosto — trigger presente,
dueños alineados, y el `INSERT` fallando por otra causa — que se ve al próximo reinicio, en el
conteo de eventos de esta misma línea.

## Verificación contra base real

En este repositorio ningún test toca una base de datos: todos usan cursores falsos. Los
tests de `tests/test_desconexiones.py` verifican el **texto** del DDL. El comportamiento se
verificó a mano contra `whaticket_copia` (172.17.0.2), ejecutando el mismo código que corre
el worker.

| Caso | Esperado | Resultado |
|---|---|---|
| `UPDATE` con el mismo valor de `status` | 0 filas | **0** |
| `UPDATE` con valor distinto | 1 fila | **1** |
| Fila resultante | ambos estados y ambos relojes poblados | `('offline','online',True,True)` |
| `INSERT ... ON CONFLICT DO UPDATE` (patrón real del ETL) | 1 fila | **1** |
| Tabla de auditoría ausente → `UPDATE` de `users` | sigue funcionando | **OK, con `WARNING`** |
| DDL fallido **sin** savepoint → resto de la transacción | se aborta | **`InFailedSqlTransaction`** |
| DDL fallido **con** savepoint | la transacción sobrevive | **OK, `False` + log** |
| `INSERT` como rol sin privilegios, **SECURITY INVOKER** | 1 fila | **0 — bug** |
| `INSERT` como rol sin privilegios, **SECURITY DEFINER** | 1 fila | **1** |
| Colgar la función de la tabla de otro rol, **antes** del `REVOKE` | 0 filas | **1 — envenenado** |
| Colgar la función de la tabla de otro rol, **después** del `REVOKE` | 0 filas | **0, `InsufficientPrivilege`** |
| Rol del ETL con `EXECUTE=False` → ¿sigue capturando? | 1 fila | **1** |
| Dueño de la tabla distinto al de la función | `False` + fila en `errors` | **`False`** |
| `asegurar_sin_romper` con `autocommit=True` | no levanta | **OK, corre sin savepoint** |
| `_DUENOS_SQL` **por nombre**, con homónimo en otro schema | 1 fila | **`CardinalityViolation` + txn abortada** |
| `_DUENOS_SQL` **por OID**, con homónimo en otro schema | 1 fila | **`(True,)`, txn viva** |
| Objeto ausente, comparando `is False` | no verde | **`(None,)` → devolvía `True`** |
| Objeto ausente, con `None` explícito | no verde | **`None` → `False`** |
| `_en_savepoint` con una consulta que revienta | contiene el fallo | **`ok=False`, txn viva** |
| Secuencia **ligada**, intentar cambiarle el dueño | — | **`FeatureNotSupported`: imposible** |
| Secuencia **desligada** con otro dueño | detectado | **`False`** |

> **La copia con superusuario no es evidencia de que producción funcione.** El rol de
> `whaticket_copia` es `whaticket`: superusuario **y** dueño de `users`. Es el mejor caso
> posible, y daba verde en todo mientras el bug de `SECURITY INVOKER` estaba presente.

### Qué verificar después del deploy

**No alcanza con contar `pg_trigger`**: ese chequeo devolvía `1` con la captura
completamente muerta. Hay que verificar que **aterriza una fila**.

```sql
-- 1. La funcion corre con los privilegios de su dueño?
SELECT prosecdef AS security_definer, proconfig AS search_path
  FROM pg_proc WHERE proname = 'registrar_conexion_operador';   -- esperado: true, no NULL

-- 2. Funcion y tabla comparten dueño? Si no, el trigger existe y NO inserta,
--    y `prosecdef` de arriba sigue diciendo true.
SELECT (SELECT proowner FROM pg_proc  WHERE proname = 'registrar_conexion_operador')
     = (SELECT relowner FROM pg_class WHERE relname = 'conexiones_operador')
       AS dueno_coincide;                                        -- esperado: true

-- 3. Esta capturando de verdad? (a los ~5 min de arrancar ya deberia haber eventos)
SELECT count(*) AS eventos, max(detected_at) AS ultimo FROM conexiones_operador;

-- 4. Si el punto 3 da 0, revisar el privilegio de DEPLOY (distinto del de ejecucion)
SELECT current_user,
       has_table_privilege(current_user, 'users', 'TRIGGER') AS puede_trigger,
       has_schema_privilege(current_user, 'public', 'CREATE') AS puede_funcion;
```

Si el punto 4 da `false`, en el log del worker aparece `[desconexiones] captura NO
aplicada, las alertas siguen (...)`. Se arregla con un `GRANT TRIGGER ON users TO <rol>`
desde el dueño; no hace falta tocar código.

## La cadencia del sync: **~300 s**, no un día

Medido el 2026-09-03 con el par `last_seen` / `refreshed_at`:

| cuenta | `max(last_seen)` | `refreshed_at` | latencia |
|---|---|---|---|
| `sistemas` | 14:21:09.309 | 14:21:10.332 | **1,02 s** |
| `datos` | 14:20:01.432 | 14:23:59.771 | **238 s** |

Consistente con `LOOKUP_REFRESH_SECONDS = 300` (default del ETL, sin *override* en
`docker-compose.yml`). Con un sync diario esos deltas serían de horas, no de un segundo.

> **Error de medición a no repetir.** Una versión anterior de este documento afirmaba una
> cadencia de ~1 vez por día, apoyada en dos lecturas de `refreshed_at` (2026-09-02 14:28 y
> 2026-09-03 14:21). Esos dos puntos **no miden la cadencia: miden cuándo se miró.**
> `refreshed_at` se pisa entero en cada pasada, así que siempre se ve un solo timestamp por
> cuenta — con cadencia de 5 minutos o de 24 horas el snapshot es idéntico. La métrica era
> incapaz de distinguir las dos hipótesis, y estar 24 h separadas era un artefacto de haber
> consultado a la misma hora del día.
>
> La lección: antes de inferir de una serie, preguntar si la métrica puede distinguir la
> hipótesis de su alternativa. El par `last_seen` / `refreshed_at` sí puede; un
> `refreshed_at` suelto no.

Guardar los **dos relojes** fue la decisión de diseño correcta, y es lo que contestó la
pregunta. `last_seen` es cuándo lo vio whaticket; `detected_at` es cuándo lo vimos nosotros.

**Consecuencia práctica:** una alerta de desconexión es viable con una latencia de ~5
minutos. No hace falta cambiar nada en el ETL, ni agregar `sync_state:users_watermark`.

## Limitación conocida: el trigger no captura `INSERT`

`AFTER UPDATE OF status` no cubre el primer `status` de un operador nuevo, así que el
histórico tiene ese hueco en la línea base.

Es deliberado. La aparición de un operador nuevo **no es una desconexión**, y meterla en la
misma tabla inyectaría eventos falsos en el *outbox* de la alerta (un usuario que aparece
con `status='offline'` se leería como que se acaba de desconectar). `users.captured_at` ya
registra cuándo se lo vio por primera vez. Si alguna vez hace falta la línea base, va en un
trigger `AFTER INSERT` separado — no se puede reusar el mismo, porque su cláusula `WHEN`
referencia `OLD`, que en un `INSERT` no existe.

## Pendiente en el repositorio del ETL

**Nada bloqueante.** Las dos tareas que este documento pedía antes (subir la frecuencia del
sync y agregar `sync_state:users_watermark`) se cayeron al medir la cadencia real: ya son
300 s, y el par `last_seen`/`refreshed_at` contesta la pregunta de latencia sin telemetría
nueva.

Queda una sola advertencia: **un `DROP TABLE users` se lleva el trigger puesto.** Se
restablece en el ciclo siguiente del worker (60 s), pero en el intervalo se pierden eventos.
Verificado que hoy no hay `DROP TABLE` ni `TRUNCATE` en `db/`, `monitor/` ni `scripts/` del
ETL, así que el riesgo es teórico.

## Por qué la alerta todavía no se diseñó

Ya no es por latencia — con 300 s de cadencia la alerta es perfectamente viable. Es por el
**contenido**.

Medición del 2026-09-03 sobre los 49 operadores: **20 de 49 `last_seen` (40,8%) caen en el
minuto `HH:00`–`HH:04`.** Si las desconexiones fueran humanas y uniformes en el reloj, se
esperaría 8,3% (5 minutos de 60). Es ~5 veces más.

Interpretación: una porción grande de las "desconexiones" son cierres automáticos de sesión,
no personas cerrando sesión. Alertar por todas llenaría el canal de no-eventos. Este
proyecto ya pagó ese error dos veces: la alerta de espera disparó 0 de 207 veces y se dio de
baja, y el `PUT` de `operator_status` devolvía 200 mintiendo.

**La regla se escribe cuando haya historial propio.** Con dos o tres días de
`conexiones_operador` se puede ver el patrón de las automáticas con datos en lugar de con
una inferencia sobre 49 filas, y decidir si el filtro es por hora, por duración de la
desconexión o por operador.

## Consultas útiles

Desconexiones de una cuenta, lo más reciente primero (usa el índice parcial):

```sql
SELECT operator_name, status_ant, status_nuevo,
       last_seen   AT TIME ZONE 'America/Guayaquil' AS last_seen_ec,
       detected_at AT TIME ZONE 'America/Guayaquil' AS detectado_ec,
       detected_at - last_seen AS latencia_del_sync
  FROM conexiones_operador
 WHERE account = 'datos' AND status_nuevo = 'offline'
 ORDER BY detected_at DESC
 LIMIT 50;
```

Latencia real del sync del ETL, ahora con historial propio:

```sql
SELECT count(*) AS eventos,
       min(detected_at - last_seen) AS minima,
       avg(detected_at - last_seen) AS promedio,
       max(detected_at - last_seen) AS maxima
  FROM conexiones_operador;
```


## La alerta

El drenaje del *outbox*: el trigger escribe en `conexiones_operador` y el hilo de alertas
manda. Vive en `src/desconexiones.py` (qué fila merece aviso) + un bloque en
`alertas.barrer` (el envío, con la maquinaria ya probada).

### El mismo chat que las alertas VIP, con interruptor propio

**Decisión del negocio (2026-09-03): un solo grupo.** El aviso tiene otro propósito, no otro
destino. Así que el canal sale de `alertas.canal_desde_env` y **no tiene variables propias**:
dos pares de variables apuntando al mismo chat se desincronizan el día que se rote el token,
y nadie se entera hasta que una de las dos alertas deja de llegar.

**El canal se comparte; el interruptor no.**

```
ALERTA_DESCONEXION=true     # vacío o ausente = APAGADO
```

Y eso no es celo: el canal VIP **ya está configurado en producción**, así que sin el flag esta
alerta se prendería sola en el momento del deploy — con un volumen estimado de 50 a 150 avisos
por día que todavía no se midió. Mismo patrón *opt-in* que `SCORING_ENABLED` y `API_DOCS`: el
default tiene que ser el seguro, no el cómodo.

En el arranque el worker loguea `[worker] alerta de desconexion: on|off`.

### Compartir chat tiene dos consecuencias, y las dos se resolvieron

**1. El mensaje tiene que ser inconfundible.** Quien lee el grupo tiene que distinguir de un
vistazo un aviso de desconexión de un resumen VIP, sin abrir nada. Los resúmenes abren con 🍀
y la marca de espera con ⏳; este abre con **🔌** y dice qué es en la primera línea, que es lo
único que se ve en la notificación del teléfono.

```
🔌 <b>Operador desconectado</b>          🍀 <b>Conversación cerrada</b>

🧑‍💼 <b>Arturo</b>  ·  cuenta datos       👤 <b>brysuye</b>  <code>#3</code>  Sur
🕐 15:01:29 (Ecuador)                    🔎 cuenta N/D
⏱ detectado 2 min despues                🧑‍💼 Genessis · 📌 soporte_cuenta
                                         ★★★★☆  4 de 5
```

**2. El tope bajó de 10 a 5 por ciclo.** El tope es *por cuenta* y hay dos, así que 10 serían
20 mensajes por ciclo de 60 s — justo en el límite de Telegram para un grupo (~20/min) y sin
dejar lugar a los resúmenes VIP, que van al mismo lado. Un 429 no pierde el aviso
(`REINTENTAR` lo desmarca y vuelve al ciclo siguiente), pero llenar la cuota con desconexiones
**retrasa los resúmenes**, que son los que alguien puede querer accionar el mismo día.

### Qué se filtra, y por qué

| Filtro | Por qué | Verificado |
|---|---|---|
| `status_nuevo = 'offline'` | una reconexión no es una alerta; avisar de las dos duplica el volumen | reconexión excluida |
| `detected_at >` ahora − **30 min** | **la lección del apagón**: el worker estuvo 93 min caído; sin ventana, al volver dispara todo el acumulado de un saque. El sembrado de `ledger_vacio` solo protege el *primer* arranque de la historia, no cada reinicio | evento de 90 min excluido |
| `LIMIT 5` por ciclo (por cuenta) | un pico (reinicio del CRM que desloguea a todos) no puede volcar cien mensajes. Lo que sobra no se pierde: entra al ciclo siguiente, 60 s después | 15 sembrados → tope respetado |
| no está en `operator_status` con `activo = false` | baja lógica del tablero: esa persona no está trabajando | `RAMIREZ` en el historial vs `Ramirez` en la tabla → excluido |
| `LEFT JOIN alertas_enviadas` | idempotencia; la clave es el **evento**, no el operador | marcada una vez, no vuelve |

> El filtro de `operator_status` **matchea por clave** (`identidad.clave_sql`: minúsculas,
> sin tildes), no por string exacto. Es el bug que ya se pagó el 2026-08-27: la tabla tenía
> `'RAMIREZ'`, el modal mandaba `'Ramirez'`, el `ON CONFLICT` no matcheaba y el operador no
> se prendía nunca — con 222 sesiones recientes. Verificado con las dos grafías.

### Cómo prender la alerta

**Medí el volumen primero.** El historial se llena con la alerta apagada, así que la
respuesta ya está en la base sin arriesgar nada:

```sql
-- Cuantos avisos por dia tendrias HOY, con los filtros puestos
SELECT date_trunc('day', c.detected_at AT TIME ZONE 'America/Guayaquil') AS dia,
       count(*) AS avisos
  FROM conexiones_operador c
 WHERE c.status_nuevo = 'offline'
   AND NOT EXISTS (SELECT 1 FROM operator_status os
                    WHERE os.account = c.account AND os.activo = false
                      AND lower(os.operator_name) = lower(c.operator_name))
 GROUP BY 1 ORDER BY 1 DESC;

-- Y la distribucion por hora, para decidir si hace falta un filtro de horario
SELECT extract(hour FROM c.detected_at AT TIME ZONE 'America/Guayaquil') AS hora,
       count(*)
  FROM conexiones_operador c WHERE c.status_nuevo = 'offline'
 GROUP BY 1 ORDER BY 1;
```

**Estimación previa, que es la razón del interruptor:** 49 operadores por una o tres
desconexiones diarias son entre 50 y 150 avisos por día. Eso no es un canal, es ruido — y un
canal que se ignora se lleva puestas las alertas que sí importan (la de espera disparó 0 de
207 y se dio de baja).

Si el número es alto, el filtro se decide **con esa distribución**, no con la inferencia
sobre 49 filas de `users.last_seen` que motivó todo esto. Candidatos: horario de atención,
duración mínima de la desconexión, o solo ciertos operadores.

Cuando el número cierre: `ALERTA_DESCONEXION=true` y reiniciar. El primer ciclo **siembra sin
enviar** (`ledger_vacio`), así que el backlog acumulado no sale de golpe.
