# Despliegue — Alertas de jugador VIP

Dos avisos a un grupo de Telegram cuando un jugador crítico es atendido o queda esperando.
Corre **dentro del worker de scoring** que ya existe: no hay servicio nuevo, no hay puerto
nuevo, no hay endpoint nuevo.

- `src/vip.py` — la tabla `vip_players`: a quién vigilar.
- `src/alertas.py` — el canal de Telegram, los dos mensajes y el barrido.
- `scripts/dump_jugadores_vip.py` — reporte del casino → `config/jugadores_vip.json`.
- `scripts/load_jugadores_vip.py` — el JSON → `vip_players`.

> **El JSON NO está versionado** (`.gitignore`), y no hace falta que lo esté: producción
> lee `vip_players`, nunca el archivo. El JSON es solo el papel con el que se carga la
> tabla. Se ignora porque 140 de los 334 `username` son un número de teléfono —así los
> identifica el casino— y eso no tiene por qué vivir en el repo.
>
> **Consecuencia**: la única copia de la lista fuera de la base es el CSV que manda el
> negocio. Guardarlo en un lugar compartido, no en la carpeta de descargas de alguien.
> Para cambiar la lista se regenera el JSON y se vuelve a cargar; para apagar a UNO,
> alcanza el `UPDATE` de más abajo.

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

### 4. Generar la lista contra PRODUCCIÓN

```bash
DATABASE_URL="<produccion>" python scripts/dump_jugadores_vip.py <reporte.csv>
```

Verificar en el JSON que `"origen_bd"` sea la base de producción.

### 5. Cargar la lista

```bash
DATABASE_URL="<produccion>" python scripts/load_jugadores_vip.py --dry-run   # mirar
DATABASE_URL="<produccion>" python scripts/load_jugadores_vip.py --pisar     # cargar
```

`--seed` (por defecto) solo llena huecos y respeta lo que alguien haya apagado a mano.
`--pisar` hace que el archivo gane. `--podar` borra lo que el archivo ya no trae — va
aparte porque es destructivo.

### 6. Encender

Poner `TELEGRAM_TOKEN_VIP` y `TELEGRAM_CHAT_VIP` en EasyPanel y reiniciar. En el log:

```
[worker] alertas VIP: on
[worker] sistemas: alertas VIP espera=0 resumen=3
```

---

## Qué mirar el primer día

| señal | dónde | qué significa |
|---|---|---|
| `alertas VIP: off` con el token puesto | log del worker | la variable no llegó al contenedor |
| ninguna alerta en horas | `SELECT count(*) FROM vip_players WHERE es_vip` | 0 = la lista no se cargó |
| `telegram 400 (NO se reintenta)` | log | bug de formato: se descarta a propósito, no reintenta |
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
