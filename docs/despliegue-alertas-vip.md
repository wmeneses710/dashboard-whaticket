# Despliegue — Alertas de jugador VIP

Dos avisos a un grupo de Telegram cuando un jugador crítico es atendido o queda esperando.
Corre **dentro del worker de scoring** que ya existe: no hay servicio nuevo, no hay puerto
nuevo, no hay endpoint nuevo.

- `src/vip.py` — la tabla `vip_players`: a quién vigilar.
- `src/alertas.py` — el canal de Telegram, los dos mensajes y el barrido.
- `scripts/dump_jugadores_vip.py` — reporte del casino → `config/jugadores_vip.json`.
- `scripts/load_jugadores_vip.py` — el JSON → `vip_players`.

> **El JSON SÍ está versionado**, y por eso lleva lo mínimo:
>
> - **entra**: `username`, `player_id`, `agencia`, `rank`, `motivo` y los `contact_id`.
> - **no entra**: el teléfono, ni `ggr_casa`, `turnover`, `depositos`, `retiros`, `kyc`.
>   Eso venía del reporte y **no lo lee nadie** —ni el loader ni los mensajes—, pero es lo
>   más sensible que había. Lo que no se usa no se guarda.
>
> Versionarlo hace que actualizar la lista sea un commit y un redeploy, con historial de
> quién entró y quién salió. Producción igual lee `vip_players`, nunca el archivo.

---

## Lo que hay que saber ANTES de tocar nada

**1. Con el token vacío no pasa nada, y eso es a propósito.**
Sin `TELEGRAM_TOKEN_VIP` o sin `TELEGRAM_CHAT_VIP` el canal calla y el barrido se saltea
sin error. Se puede subir el código, ver el log decir `alertas VIP: off`, y recién después
crear el grupo. **Ese es el orden recomendado.**

**2. Las tablas se crean solas.**
`alertas_enviadas` y `vip_players` son self-healing: el primer barrido las crea. No hay
migración manual ni ventana de mantenimiento. Ensayado contra una base vacía.

**3. El `contact_id` es un uuid de UNA base.**
El JSON se genera contra la copia porque es donde se investiga, pero **los uuid tienen que
ser los de producción**. Por eso el JSON estampa `origen_bd` y el loader se planta si no
coincide. Si ese guard salta, no lo esquives: volvé a correr el dump apuntando a producción.

**4. El aviso de espera NO es en tiempo real, y hay que decirlo así.**
MEDIDO: el ETL tarda p50 **9 minutos** en traernos un mensaje del cliente en conversación
viva, y en el 66% de los casos lo trae junto con la respuesta del operador. Por eso:
- el umbral es **15 minutos**, no 5 (a 5 el mensaje mentiría: promete 5 y dispara a los 9);
- el mensaje dice **a qué hora escribió el cliente**, para que nadie asuma que es en vivo;
- se agarran el **54%** de las esperas de 15–60 min y el **89%** de las de más de una hora;
- la espera de 6–12 minutos **no se agarra**. Con este ETL no hay forma.

La solución de raíz es que el ETL ingiera el mensaje del cliente por sí solo, sin esperar
que el operador toque el ticket. Es un pedido a ese equipo, no un cambio de este repo.

---

## Pasos

### 1. Crear el bot y el grupo
1. `@BotFather` → `/newbot` → guardar el token.
2. Crear el grupo y agregar al bot.
3. Sacar el `chat_id` del grupo (empieza con `-`).

### 2. Validar token y chat SIN escribir en el grupo

```bash
curl -s "https://api.telegram.org/bot$TOKEN/getMe"
curl -s "https://api.telegram.org/bot$TOKEN/getChat?chat_id=$CHAT"
```

Los dos tienen que devolver `"ok":true`. Esto no manda ningún mensaje.

### 3. Subir el código con las alertas APAGADAS

Desplegar sin `TELEGRAM_TOKEN_VIP` ni `TELEGRAM_CHAT_VIP`. En el log del worker:

```
[worker] alertas VIP: off
```

Las dos tablas quedan creadas y vacías. Nada se envía.

### 4. La lista se siembra sola

**No hay paso manual.** El JSON está versionado, `.dockerignore` no excluye `config/`, así
que entra a la imagen; y `seed_vip_players()` corre en cada arranque del contenedor, igual
que `seed_operator_status()`. **Commit + redeploy y la lista está.**

En el log del arranque:

```
INFO vip_players: 255 filas sembradas · vinculos vivos en esta base: 255 de 255
```

**Lo único que hay que mirar son los vínculos vivos.** Si dice `0 de 255`, los `contact_id`
son de otra base y no va a sonar una sola alerta — y sin ese número eso se vería idéntico a
un día tranquilo. Por eso además loguea un `WARNING` explícito debajo de la mitad.

La siembra **NO PISA** (`ON CONFLICT DO NOTHING`), mismo contrato que `operator_status`: si
alguien apagó un VIP en producción, un deploy no puede volver a encenderlo. Ensayado.

#### Cuando cambia la lista

```bash
DATABASE_URL="<la copia>" python scripts/dump_jugadores_vip.py <reporte-nuevo.csv>
git add config/jugadores_vip.json && git commit -m "chore(vip): lista de <fecha>"
```

Redeploy y listo. Los `contact_id` de la copia sirven en producción porque la copia es un
**restore del dump de EasyPanel**: son los mismos uuid. El arranque lo verifica igual.

#### Cuando hace falta que el archivo GANE

La siembra solo llena huecos. Para pisar lo que esté en la base:

```bash
C=$(docker ps --format '{{.Names}}' | grep -i whaticket-dashboard | head -1)
docker exec "$C" python scripts/load_jugadores_vip.py --dry-run   # mirar
docker exec "$C" python scripts/load_jugadores_vip.py --pisar     # pisar
docker exec "$C" python scripts/load_jugadores_vip.py --podar     # borrar lo que ya no está
```

El archivo ya viaja dentro de la imagen: no hace falta `scp` ni `docker cp`.

#### Lo que NO hay que hacer

Publicar el puerto 5432 hacia afuera. Docker publica saltando el firewall del host (sus
reglas van a la cadena `DOCKER` de iptables, no a `INPUT`), así que un `ufw deny 5432`
**no lo tapa**.

### 5. Encender

Poner `TELEGRAM_TOKEN_VIP` y `TELEGRAM_CHAT_VIP` en EasyPanel y reiniciar. En el log:

```
[worker] alertas VIP: on
[worker] sistemas: alertas VIP espera=0 resumen=3
```

---

## Logging

Todo sale por **stdout/stderr del contenedor**, en la misma corriente que el resto del
worker y con su timestamp.

Al arrancar, una vez:

```
[worker] alertas VIP: on          ← o `off` si falta el token o el chat
```

Por ciclo, **solo si pasó algo** (incluido si falló):

```
[worker] sistemas: alertas VIP espera=0 resumen=3 fallos=0
```

Cuando un envío falla, una línea por alerta:

```
[alertas] sistemas resumen 3f2a…: reintentar     ← red, 429 o 5xx: se desmarca y reintenta
[alertas] sistemas espera tkt-1:…: descartar     ← 4xx de formato: NO se reintenta
[alertas] barrido sistemas ROTO: OperationalError: ...   ← el barrido entero
```

> **`fallos` existe por un agujero real**: antes se devolvía solo lo enviado, así que un
> ciclo con el canal caído daba los mismos ceros que un día tranquilo y no escribía una
> sola línea. Un canal muerto se veía idéntico a que no hubiera pasado nada.

`barrer` **nunca lanza**: el scoring es el producto, la alerta es el aviso.

## Qué mirar el primer día

| señal | dónde | qué significa |
|---|---|---|
| `alertas VIP: off` con el token puesto | log del worker | la variable no llegó al contenedor |
| ninguna línea `alertas VIP` en horas | log | no hubo nada que avisar **ni** fallos |
| `fallos=` distinto de 0 | log | mirar la línea `[alertas]` de arriba |
| ninguna alerta y `fallos=0` | `SELECT count(*) FROM vip_players WHERE es_vip` | 0 = la lista no se cargó |
| `descartar` repetido | log | bug de formato: se descarta a propósito |
| avisos repetidos | `alertas_enviadas` | el ledger no está persistiendo |

---

## Apagar

Vaciar `TELEGRAM_TOKEN_VIP` y reiniciar. El worker sigue scoreando igual: `barrer` no
lanza nunca, porque el scoring es el producto y la alerta es el aviso.

Para apagar UN jugador sin perder su referencia:

```sql
UPDATE vip_players SET es_vip = false WHERE username = '<username>';
```

---

## Lo que NO entra en este despliegue

- **El resumen de nota baja** se marca con las estrellas pero sigue siendo un resumen.
- **Los 13 vínculos de confianza `baja`** no están en la tabla: son menciones del username
  en varios contactos (`quezada` cae en 20, `medardo` en 10, porque son apellidos).
  Quedan listados en el JSON para que se vea que fueron evaluados.
- **Los 79 sin vínculo**: 32 son teléfonos que nunca nos escribieron, 47 usernames que no
  aparecen en ningún mensaje.
