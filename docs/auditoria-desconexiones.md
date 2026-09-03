# Auditoría de conexión/desconexión de operadores

Estado al 2026-09-03: **la captura está construida y verificada. La alerta no** —
deliberadamente, ver el último apartado.

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
en cada ciclo del worker (60 s). Las cuatro sentencias son `IF NOT EXISTS` / `OR REPLACE`.

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

## Los dos modos de falla silenciosa, y cómo se cubren

Son **dos**, en momentos distintos, y confundirlos fue el error de la primera versión de
este documento.

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

No se hace `REVOKE EXECUTE FROM PUBLIC`: una función de trigger invocada directamente falla
con *"trigger functions can only be called as triggers"*, así que no hay camino de escalada,
y el `REVOKE` rompería la invariante de que toda sentencia del DDL sea idempotente.

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

La cobertura correcta es una **sonda del lado del dashboard**: si `asegurar_sin_romper`
devolvió `True` (el trigger está) y `conexiones_operador` sigue vacía después de varias
pasadas del ETL, registrar en `errors`. Detecta todas las causas de vacío silencioso, no
solo la de privilegios. **Está pendiente** — hoy la detección es el punto 2 del chequeo
post-deploy, hecho a mano.

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

-- 2. Esta capturando de verdad? (a los ~5 min de arrancar ya deberia haber eventos)
SELECT count(*) AS eventos, max(detected_at) AS ultimo FROM conexiones_operador;

-- 3. Si el punto 2 da 0, revisar el privilegio de DEPLOY (distinto del de ejecucion)
SELECT current_user,
       has_table_privilege(current_user, 'users', 'TRIGGER') AS puede_trigger,
       has_schema_privilege(current_user, 'public', 'CREATE') AS puede_funcion;
```

Si el punto 3 da `false`, en el log del worker aparece `[desconexiones] captura NO
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
