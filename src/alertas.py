"""Las dos alertas de jugador VIP, a Telegram.

QUE PIDIO EL NEGOCIO (2026-08-26), y son DOS disparos distintos:

  ESPERA   el cliente VIP lleva mas de 5 minutos sin respuesta, EN HORARIO de atencion.
  RESUMEN  termino una conversacion suya: quien la atendio, para que, la calificacion,
           la duracion y el motivo.

EL MECANISMO DE ENVIO SALE DE `grafana-llm-alertas`, que ya manda a Telegram en
produccion. Se copio tal cual lo que alli ya esta probado:

  * un POST a `api.telegram.org/bot{token}/sendMessage`, `timeout=10`, que devuelve
    bool y NUNCA lanza. El scoring es el producto; la alerta es un aviso, y un aviso que
    revienta no puede tumbar el worker.
    CON `httpx` Y NO `requests`: alla usan `requests`, aca ya esta `httpx` (src/llm.py) y
    la forma de la llamada es la misma. Una alerta no justifica una dependencia nueva.
  * Si falta el token o el chat, el canal CALLA y el envio se saltea SIN error: sirve
    para desplegar en seco antes de conectar el grupo. Es textual de su `.env.example`.
    UN SOLO BOT y no dos: alla usan uno por tipo de alerta, pero aca el negocio pidio un
    unico canal. Las dos alertas se distinguen por el emoji y el titulo (⏳ / 🍀), no por
    el destino, asi que el prefijo del mensaje es lo unico que separa una de la otra.
  * Throttle de 0,15 s entre envios (~10 msg/s, lejos del limite del Bot API), sin dormir
    en el primero.

LO QUE **NO** SE COPIO, Y ES LA DIFERENCIA DE FONDO. Alla el disparo es ENTRANTE: Grafana
hace POST y el webhook responde. Aca nadie nos avisa -- el disparo es NUESTRO, desde el
worker de scoring. Por eso la idempotencia no puede delegarse en que el emisor no
reintente: la ponemos nosotros con `alertas_enviadas`. Es el mismo problema que alla
resolvieron desacoplando en un hilo ("Grafana corta por timeout, reintenta el POST, y el
resultado son mensajes duplicados en Telegram"), pero la causa aca es otra: el barrido
corre cada 60 segundos y sin ledger manda la misma alerta cada minuto.

EL MENSAJE NO LLEVA EL TEXTO DEL CHAT, y es deliberado. El metadato alcanza para decidir
si alguien tiene que entrar; el cuerpo llevaria cedulas, cuentas y credenciales --lo mismo
que `src/censura.py` tapa en el tablero-- a un grupo de Telegram donde nadie lo tapa.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from src.horario import TZ as TZ_EC
from src.horario import en_horario, espera_efectiva

logger = logging.getLogger(__name__)

# ~10 msg/s. El limite del Bot API es mas alto; esto deja margen y ordena la salida.
THROTTLE_SEGUNDOS = 0.15

# EL UMBRAL, y lo fija el PIPELINE, no el gusto. El negocio pidio 5 minutos y el dato dice
# que a 5 minutos la alerta MIENTE: MEDIDO sobre 53.828 mensajes de cliente en conversacion
# viva, el ETL tarda p50 **9 minutos** en entregarnoslos, asi que una alerta de "5 min"
# dispara, en la mediana, cuando ya pasaron 9. A 15 minutos la mediana coincide con lo
# prometido -- es el primer valor donde el numero del mensaje es cierto.
#
#     umbral   dispara a tiempo   espera real p50 al disparar
#      5 min        47,1%                9,0 min   <- promete 5, dispara a los 9
#     15 min        57,0%               15,0 min   <- deja de mentir
#     30 min        63,5%               30,0 min
UMBRAL_ESPERA_SEGUNDOS = 900

# LA PRORROGA: cuanto tiene que pasar entre la PRIMERA vez que vemos la espera y la alerta.
# Idea del negocio, y el motivo es duro: el ETL NO entrega en orden. MEDIDO sobre 20 dias,
# el 71,6% de los mensajes llega despues de otro mas nuevo, y cuando llega tarde esta p50
# 3 min y p90 2 h atras. Que no veamos la respuesta no prueba que no exista.
# Baja las falsas alarmas de 2,06% a 1,71% de las conversaciones bien atendidas: es poco,
# y va igual porque es gratis y hace la alerta defendible.
PRORROGA_SEGUNDOS = 300

# El tipo del ledger para la PRIMERA observacion. No es una alerta: es la anotacion que
# permite confirmarla en la siguiente pasada.
TIPO_VISTA = "espera_vista"

_SIN_DATO = "N/D"

# EL RESULTADO DE UN ENVIO, en tres estados y no en un booleano. La diferencia la trajo el
# bot de verdad: un 400 por formato va a fallar IGUAL la proxima vez, y un 429 o un corte
# de red no. Con un solo `False` las dos cosas se trataban igual y la alerta se perdia.
OK = "ok"
REINTENTAR = "reintentar"   # transitorio: red, 429, 5xx. La alerta todavia sirve.
DESCARTAR = "descartar"     # permanente: 400 de formato, canal sin configurar.


# --- el canal ---------------------------------------------------------------

@dataclass(frozen=True)
class Canal:
    """Un bot de Telegram y su chat. Sin token o sin chat, CALLA (despliegue en seco)."""

    token: str
    chat_id: str

    @property
    def configurado(self) -> bool:
        return bool((self.token or "").strip() and (self.chat_id or "").strip())

    def enviar(self, texto: str) -> str:
        """`OK` / `REINTENTAR` / `DESCARTAR`. Nunca lanza: no tumba el worker.

        EN HTML Y NO EN MARKDOWN. Probado contra el bot real: `*andrea_deniss*` junto a
        `soporte_cuenta` devolvia **400 can't parse entities**, porque el username y el
        motivo traen guiones bajos y el parser intenta abrir una cursiva. HTML tiene TRES
        caracteres que escapar y Markdown dieciocho; con datos que vienen del CRM y del
        casino, el unico que se puede garantizar es HTML.

        UN 4xx NO SE REINTENTA. Es un error nuestro de formato y va a fallar igual: se
        loguea como error para que se vea, no se vuelve a intentar. Lo transitorio (429,
        5xx, red) si, porque la alerta todavia sirve.
        """
        if not self.configurado:
            return DESCARTAR
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": texto, "parse_mode": "HTML"},
                timeout=10)
            if r.status_code == 200:
                return OK
            if 400 <= r.status_code < 500 and r.status_code != 429:
                logger.error("telegram %s (NO se reintenta): %s", r.status_code, r.text[:300])
                return DESCARTAR
            logger.warning("telegram %s: %s", r.status_code, r.text[:200])
            return REINTENTAR
        except Exception as e:  # noqa: BLE001 - el aviso no puede voltear el scoring
            logger.warning("telegram error: %s: %s", type(e).__name__, e)
            return REINTENTAR


def enviar_lote(canal: Canal, mensajes: list[str]) -> int:
    """Manda los mensajes con throttle. Devuelve cuantos entraron.

    NO DUERME EN EL PRIMERO: con 20 alertas eso son 3 segundos de arranque regalados.
    """
    if not canal.configurado:
        return 0
    enviados = 0
    for i, m in enumerate(mensajes):
        if i:
            time.sleep(THROTTLE_SEGUNDOS)
        if canal.enviar(m) == OK:
            enviados += 1
    return enviados


# --- la idempotencia --------------------------------------------------------

_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS alertas_enviadas (
        account    text        NOT NULL,
        tipo       text        NOT NULL,   -- 'espera' | 'resumen'
        clave      text        NOT NULL,   -- que EPISODIO, no que jugador
        enviada_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (account, tipo, clave)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alertas_enviadas_at ON alertas_enviadas (enviada_at)",
)


def ensure_table(cur) -> None:
    """Crea `alertas_enviadas` **y** `vip_players` si faltan (idempotente, self-healing).

    LA DE VIP TAMBIEN, y no es de mas: las dos consultas del barrido la usan, pero quien la
    crea es el loader (`scripts/load_jugadores_vip.py`). En una base nueva --el dia del
    despliegue-- el worker arranca antes que el loader, la consulta revienta, `barrer` se
    traga la excepcion y las alertas no funcionan NUNCA, en silencio y una vez por minuto.
    Asegurarla aca vuelve el despliegue independiente del orden: una tabla VACIA significa
    "no hay VIP que vigilar", que es lo correcto, no un error.
    """
    from src.vip import ensure_table as ensure_vip
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)
    ensure_vip(cur)


def clave_espera(ticket_id: str, ultimo_cliente_at: datetime) -> str:
    """El EPISODIO de espera, no el ticket.

    Si la clave fuera solo el ticket, un cliente que vuelve a esperar mañana en la misma
    conversacion no volveria a alertar NUNCA. El instante del ultimo mensaje del cliente
    es lo que distingue una espera de la siguiente.
    """
    return f"{ticket_id}:{ultimo_cliente_at.isoformat()}"


def clave_resumen(session_id: str) -> str:
    """La SESION. Un re-scoreo masivo --como el de v22-- no puede volver a avisar de una
    charla de hace un mes."""
    return str(session_id)


def desmarcar(cur, account: str, tipo: str, clave: str) -> None:
    """Borra el rastro para que el proximo barrido REINTENTE.

    Solo para fallos TRANSITORIOS. Marcar antes de mandar protege del duplicado; sin este
    desmarcado, un corte de red de un segundo borra la alerta del mundo.
    """
    cur.execute("DELETE FROM alertas_enviadas WHERE account=%s AND tipo=%s AND clave=%s",
                (account, tipo, clave))


def marcar_enviada(cur, account: str, tipo: str, clave: str) -> bool:
    """Deja el rastro. False si ya estaba (o sea: no hay que mandarla)."""
    cur.execute(
        "INSERT INTO alertas_enviadas (account, tipo, clave) VALUES (%s, %s, %s) "
        "ON CONFLICT (account, tipo, clave) DO NOTHING", (account, tipo, clave))
    return cur.rowcount > 0


def ahora_de_la_base(cur) -> datetime:
    """El reloj de la BASE, que es el unico que hay que creer.

    `barrer` comparaba `datetime.now()` de la APP contra timestamps que salen de la base.
    Hoy dan igual porque la BD corre en 127.0.0.1; en produccion esta en otra maquina, y
    un desfase corre la espera de los dos lados: si la app atrasa, la alerta no suena; si
    adelanta, se infla. Un solo reloj y se acabo la clase de bug.
    """
    cur.execute("SELECT now()")
    return cur.fetchone()[0]


def confirmados(cur, account: str, candidatos: list[dict], ahora: datetime,
                prorroga_segundos: int = PRORROGA_SEGUNDOS) -> list[dict]:
    """Los episodios que ya se vieron antes y SIGUEN sin respuesta.

    LA PRIMERA VEZ NO ALERTA: se anota y se espera. Recien cuando la prorroga vencio y el
    episodio sigue apareciendo como candidato, el silencio es creible. Es la confirmacion
    en dos observaciones que pidio el negocio.

    CANCELAR ES GRATIS: si la respuesta aparece, `candidatos_espera` deja de traer el
    episodio y la anotacion queda huerfana. No hay que retractar nada.
    """
    if not candidatos:
        return []
    claves = [clave_espera(c["ticket_id"], c["ultimo_cliente_at"]) for c in candidatos]
    cur.execute("SELECT clave, enviada_at FROM alertas_enviadas "
                "WHERE account=%s AND tipo=%s AND clave = ANY(%s)",
                (account, TIPO_VISTA, claves))
    visto = dict(cur.fetchall())
    limite = timedelta(seconds=prorroga_segundos)
    listos, nuevos = [], []
    for c, clave in zip(candidatos, claves):
        cuando = visto.get(clave)
        if cuando is None:
            nuevos.append(clave)
        elif ahora - cuando >= limite:
            listos.append(c)
    for clave in nuevos:
        marcar_enviada(cur, account, TIPO_VISTA, clave)
    return listos


# --- ESPERA: el horario manda -----------------------------------------------

def filtrar_espera(candidatos: list[dict], ahora: datetime,
                   umbral_segundos: int = UMBRAL_ESPERA_SEGUNDOS) -> list[dict]:
    """Los candidatos que de verdad estan esperando demasiado.

    DOS COMPUERTAS, y las dos son de `src/horario.py`:

    1. `en_horario(ahora)`. A las 04:00 no hay nadie trabajando y avisar no sirve de nada.
    2. `espera_efectiva`, que descuenta la noche. Un cliente que escribio 23:50 y sigue
       esperando 06:05 NO lleva seis horas: lleva quince minutos de horario. Medir por
       reloj de pared dispararia una tormenta cada mañana al abrir.

    Es la misma regla que ya evita que el tablero reproche "respondio 1,7 horas despues"
    cuando fueron 8 minutos.
    """
    if not en_horario(ahora):
        return []
    out = []
    for c in candidatos:
        efectiva = espera_efectiva(c.get("ultimo_cliente_at"), ahora)
        if efectiva is None:
            continue
        seg = efectiva.total_seconds()
        if seg >= umbral_segundos:
            out.append({**c, "espera_segundos": int(seg)})
    return out


# --- de donde salen los candidatos ------------------------------------------
#
# LAS DOS CONSULTAS VIVEN ACA Y NO EN src/queries.py: aquel modulo es el del TABLERO y
# pasa por `_rows_as_dicts`, que censura. Estas alimentan un canal interno y devuelven
# metadatos, no texto de chat.

# EL ULTIMO MENSAJE DE LA CONVERSACION, ignorando las NOTAS del CRM. Una nota es interna:
# contarla como respuesta al cliente es el bug que `hubo_respuesta_del_negocio` ya
# documenta en src/sin_respuesta.py, y aca apagaria la alerta justo cuando hace falta.
#
# LA VENTANA DE DOS DIAS NO ES UN ATAJO: una espera mas vieja que eso ya no es una espera,
# es una conversacion abandonada, y avisar de ella a los cinco minutos de horario seria
# revivir historia. Ademas acota el scan de `messages`.
_ESPERA_SQL = """
WITH ultimo AS (
    SELECT DISTINCT ON (m.ticket_id)
           m.ticket_id, m.from_me, m.created_at
    FROM messages m
    JOIN tickets t ON t.id = m.ticket_id
    JOIN vip_players v ON v.contact_id = t.contact_id::text AND v.account = t.account
    WHERE v.es_vip
      AND t.account = %(account)s
      AND t.closed_at IS NULL
      AND NOT coalesce(t.is_group, false)
      AND coalesce(m.is_note, false) = false
      AND m.created_at > now() - interval '2 days'
    ORDER BY m.ticket_id, m.created_at DESC
)
SELECT u.ticket_id::text AS ticket_id,
       u.created_at       AS ultimo_cliente_at,
       v.username, v.ranking, v.agencia,
       v.motivo           AS motivo_vip,
       q.name             AS queue,
       usr.name           AS operador
FROM ultimo u
JOIN tickets t   ON t.id = u.ticket_id
JOIN vip_players v ON v.contact_id = t.contact_id::text AND v.account = t.account
LEFT JOIN queues q  ON q.id = t.queue_id
LEFT JOIN users usr ON usr.id = t.user_id
WHERE u.from_me = false
"""

# EL RESUMEN SALE DE LA SESION YA SCOREADA, que es lo que define "termino la conversacion":
# `worker.fetch_pending_sessions` trae sesiones CERRADAS sin scorear, y cuando el score se
# persiste ya existen quien atendio, la nota, la duracion y el motivo. Antes de eso no hay
# resumen que mandar.
#
# EL LEFT JOIN CONTRA EL LEDGER es lo que evita repetir: el barrido corre cada 60 segundos.
# Y la ventana de un dia es la red por si el ledger se vacia -- sin ella, conectar el bot
# despues de un rescore masivo dispararia miles de mensajes de charlas viejas.
_RESUMEN_SQL = """
SELECT cs.conversation_id::text AS session_id,
       v.username, v.ranking, v.agencia,
       v.motivo         AS motivo_vip,
       cs.user_name     AS operador,
       cs.motivo        AS motivo,
       cs.stars, cs.first_response_seconds, cs.resolution_seconds
FROM conversation_scores cs
JOIN tickets t     ON t.id = cs.ticket_id
JOIN vip_players v ON v.contact_id = t.contact_id::text AND v.account = cs.account
LEFT JOIN alertas_enviadas a
       ON a.account = cs.account AND a.tipo = 'resumen'
      AND a.clave = cs.conversation_id::text
WHERE v.es_vip
  AND cs.account = %(account)s
  AND a.clave IS NULL
  AND cs.eval_status = 'evaluated'
  AND cs.scored_at > now() - interval '1 day'
"""


def _dicts(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def candidatos_espera(cur, account: str) -> list[dict]:
    """Conversaciones VIP abiertas cuyo ULTIMO mensaje es del cliente.

    No filtra por tiempo: eso lo hace `filtrar_espera` con `espera_efectiva`, que es
    codigo de produccion y sabe descontar la noche. Reimplementar esa resta en SQL seria
    una segunda version del contrato.
    """
    cur.execute(_ESPERA_SQL, {"account": account})
    return _dicts(cur)


def resumenes_pendientes(cur, account: str) -> list[dict]:
    """Sesiones VIP ya calificadas que todavia no se avisaron."""
    cur.execute(_RESUMEN_SQL, {"account": account})
    return _dicts(cur)


# --- el formato: TODA cifra la escribe este modulo --------------------------
#
# La leccion es de `formatting.py` en grafana-llm-alertas: el formato repartido en cada
# f-string divergio y el mismo mensaje llego a imprimir dos notaciones a una linea de
# distancia. Y el singular sobre un solo minuto es el MISMO bug que ya arreglamos en el
# tablero el 2026-08-25 ("1 minutos").

def duracion(segundos: float | None) -> str:
    """`45 s` · `1 min` · `8 min` · `1 h 30 min` · `1 d 12 h`. Nunca "1 minutos".

    SE CUENTA EN DIAS a partir de las 24 h. Con la ventana de dos dias el tope real son
    unas 36 h de horario, pero una prueba escupio "2496 h 35 min" y eso no lo lee nadie:
    un tramo largo se dice como lo diria una persona.
    """
    if segundos is None:
        return _SIN_DATO
    try:
        s = int(float(segundos))
    except (TypeError, ValueError):
        return _SIN_DATO
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        horas, resto = divmod(s, 3600)
        minutos = resto // 60
        return f"{horas} h" if not minutos else f"{horas} h {minutos} min"
    dias, resto = divmod(s, 86400)
    horas = resto // 3600
    return f"{dias} d" if not horas else f"{dias} d {horas} h"


def _esc(v) -> str:
    """Todo dato que viene de afuera pasa por aca. Son tres caracteres: & < >."""
    return html.escape(str(v), quote=False)


def _quien(v) -> str:
    """Un campo vacio se escribe, no se imprime `None` en un grupo de Telegram."""
    return _esc(str(v).strip()) if v not in (None, "") else "sin asignar"


def _titulo(d: dict) -> str:
    puesto = f" #{_esc(d['ranking'])}" if d.get("ranking") else ""
    agencia = f" · {_esc(d['agencia'])}" if d.get("agencia") else ""
    return f"<b>{_esc(d.get('username') or _SIN_DATO)}</b>{puesto}{agencia}"


def _hora_ec(cuando) -> str:
    """La hora local de Ecuador, que es donde trabaja el que lee la alerta."""
    if not isinstance(cuando, datetime):
        return _SIN_DATO
    return cuando.astimezone(TZ_EC).strftime("%H:%M")


def mensaje_espera(d: dict) -> str:
    """ALERTA. Grita, y la primera linea carga lo que se lee en la notificacion.

    LLEVA LA HORA A LA QUE ESCRIBIO EL CLIENTE, y no es adorno. MEDIDO: el 42,6% de estas
    alertas puede dispararse tarde por el retraso del ETL (p75 39 min, p90 2 h). Si el
    mensaje solo dijera "lleva 20 min", el que lo lee asume que es en vivo. La hora lo
    desmiente sin que nadie tenga que conocer el pipeline.
    """
    hora = d.get("ultimo_cliente_at")
    cuerpo = [
        f"👤 <b>{_esc(d.get('username') or _SIN_DATO)}</b>"
        f"  <code>#{_esc(d.get('ranking') or '?')}</code>  {_esc(d.get('agencia') or _SIN_DATO)}",
        f"📋 {_quien(d.get('queue'))} · 🧑‍💼 {_quien(d.get('operador'))}",
    ]
    if isinstance(hora, datetime):
        cuerpo.append(f"🕐 Escribió {_hora_ec(hora)}")
    cuerpo.append(f"⭐ VIP por {_esc(d.get('motivo_vip') or _SIN_DATO)}")
    return "\n".join(
        [f"🚨 <b>ATENCIÓN — VIP esperando {duracion(d.get('espera_segundos'))}</b>", ""]
        + cuerpo)


def _estrellas(v) -> str:
    """`★★★★☆`. Se lee de un vistazo; "4 de 5" hay que leerlo."""
    try:
        n = max(0, min(5, int(round(float(v)))))
    except (TypeError, ValueError):
        return "☆☆☆☆☆"
    return "★" * n + "☆" * (5 - n)


def mensaje_resumen(d: dict) -> str:
    """RESUMEN. Es un REGISTRO: se lee sin apuro y no pide hacer nada.

    LA NOTA BAJA SE MARCA PERO NO SE VUELVE ALERTA. Un resumen de 2 estrellas sigue siendo
    info --nadie tiene que correr-- pero es la info que alguien deberia mirar, asi que las
    estrellas la delatan de un vistazo sin cambiarle el tono al mensaje.
    """
    estrellas = d.get("stars")
    nota = f"{estrellas:g} de 5" if estrellas is not None else "sin nota"
    return "\n".join([
        "🍀 <b>Conversación cerrada</b>",
        "",
        f"👤 <b>{_esc(d.get('username') or _SIN_DATO)}</b>"
        f"  <code>#{_esc(d.get('ranking') or '?')}</code>  {_esc(d.get('agencia') or _SIN_DATO)}",
        f"🧑‍💼 {_quien(d.get('operador'))} · 📌 {_esc(d.get('motivo') or _SIN_DATO)}",
        f"{_estrellas(estrellas)}  {nota}",
        f"⏱ 1ª respuesta {duracion(d.get('first_response_seconds'))}"
        f" · duró {duracion(d.get('resolution_seconds'))}",
        f"⭐ VIP por {_esc(d.get('motivo_vip') or _SIN_DATO)}",
    ])


# --- el barrido: lo unico que el worker necesita llamar ----------------------

def barrer(conn, account: str, canal: Canal,
           ahora: datetime | None = None,
           umbral_segundos: int = UMBRAL_ESPERA_SEGUNDOS,
           log=None) -> dict:
    """Un ciclo de alertas de UNA cuenta. Devuelve `{"espera": n, "resumen": n}`.

    EL ORDEN DE CADA ALERTA ES: marcar primero, mandar despues. Al reves, un fallo de red
    despues del envio dejaria la alerta sin rastro y el proximo barrido --sesenta segundos
    mas tarde-- la repetiria. Se prefiere perder un aviso a repetirlo: el canal que grita
    dos veces por lo mismo se deja de leer, y eso apaga las dos alertas a la vez.

    NO LANZA NUNCA. El worker lo llama dentro de su ciclo de scoring; una alerta rota no
    puede dejar sesiones sin calificar.
    """
    # `fallos` NO es cosmetico. Antes se devolvia solo lo ENVIADO: si los diez envios del
    # ciclo fallaban, esto daba {espera:0, resumen:0}, que es lo MISMO que devuelve un dia
    # tranquilo. El worker solo loguea cuando hay algo, asi que un canal caido se veia
    # identico a que no hubiera pasado nada.
    hecho = {"espera": 0, "resumen": 0, "fallos": 0}
    # El worker escribe con timestamp por `emit`; el `logger` de la libreria sale por
    # stderr sin hora ni nombre y en un log de contenedor mezclado con uvicorn no se
    # rastrea. Con el `log` inyectado la falla aparece en la misma corriente que el resto.
    decir = log or (lambda m: logger.warning("%s", m))
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            # EL RELOJ SALE DE LA BASE, no de la app. Es el mismo reloj con el que se
            # escribieron los timestamps que vamos a restar.
            if ahora is None:
                ahora = ahora_de_la_base(cur)
        conn.commit()

        # ESPERA. La compuerta del horario va ANTES de la consulta: un barrido cada 60 s
        # durante las 6 horas de cierre son 360 consultas para nada.
        if canal.configurado and en_horario(ahora):
            with conn.cursor() as cur:
                candidatos = candidatos_espera(cur, account)
            # LA CONFIRMACION EN DOS OBSERVACIONES va DESPUES del umbral y del horario:
            # solo se anota lo que ya califica, no todo lo que respira.
            listos = filtrar_espera(candidatos, ahora, umbral_segundos)
            with conn.cursor() as cur:
                listos = confirmados(cur, account, listos, ahora)
            conn.commit()
            for c in listos:
                clave = clave_espera(c["ticket_id"], c["ultimo_cliente_at"])
                with conn.cursor() as cur:
                    nueva = marcar_enviada(cur, account, "espera", clave)
                conn.commit()
                if not nueva:
                    continue
                estado = canal.enviar(mensaje_espera(c))
                if estado == OK:
                    hecho["espera"] += 1
                else:
                    hecho["fallos"] += 1
                    decir(f"[alertas] {account} espera {clave}: {estado}")
                if estado == REINTENTAR:
                    with conn.cursor() as cur:
                        desmarcar(cur, account, "espera", clave)
                    conn.commit()
                time.sleep(THROTTLE_SEGUNDOS)

        # RESUMEN. No mira el horario: una conversacion que cerro 23:50 se avisa igual.
        if canal.configurado:
            with conn.cursor() as cur:
                pendientes = resumenes_pendientes(cur, account)
            for r in pendientes:
                with conn.cursor() as cur:
                    nueva = marcar_enviada(cur, account, "resumen", clave_resumen(r["session_id"]))
                conn.commit()
                if not nueva:
                    continue
                estado = canal.enviar(mensaje_resumen(r))
                if estado == OK:
                    hecho["resumen"] += 1
                else:
                    hecho["fallos"] += 1
                    decir(f"[alertas] {account} resumen {r['session_id']}: {estado}")
                if estado == REINTENTAR:
                    with conn.cursor() as cur:
                        desmarcar(cur, account, "resumen", clave_resumen(r["session_id"]))
                    conn.commit()
                time.sleep(THROTTLE_SEGUNDOS)
    except Exception as e:  # noqa: BLE001 - el scoring es el producto, la alerta es el aviso
        hecho["fallos"] += 1
        decir(f"[alertas] barrido {account} ROTO: {type(e).__name__}: {e}")
    return hecho


def canal_desde_env(env) -> Canal:
    """El bot de las alertas VIP. UNO solo: las dos alertas van al mismo grupo.

    Vacio = apagado, y el barrido lo saltea sin error: se puede subir el worker con las
    alertas puestas y el grupo todavia sin crear.
    """
    return Canal(env.get("TELEGRAM_TOKEN_VIP", ""), env.get("TELEGRAM_CHAT_VIP", ""))
