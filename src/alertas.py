"""La alerta de jugador VIP, a Telegram.

QUE PIDIO EL NEGOCIO (2026-08-26):

  RESUMEN  termino una conversacion suya: quien la atendio, para que, la calificacion,
           la duracion y el motivo.

ERAN DOS. La otra --ESPERA, "el cliente VIP lleva mas de 5 minutos sin respuesta"-- se
BORRO el 2026-08-31, medida contra 30 dias de datos reales. En horario de atencion y con
el umbral de 5 minutos que pidio el negocio, **132 esperas lo superaron y solo 3 habrian
llegado**:

    100 de 132   el operador contesto ANTES de que la alerta pudiera existir. La espera
                 mediana de este grupo es 6,6 min y el piso de la alerta son 10 (5 de
                 umbral + 5 de prorroga): el aviso nace tarde por aritmetica.
     29 de 132   el ETL nos entrego el mensaje del cliente cuando ya estaba atendida
                 (mediana de captura: 26 min).
      3 de 132   llegaban, y solo porque ese dia el ETL tardo 0,4 min.

NO SE ARREGLA MOVIENDO EL UMBRAL: a 30 minutos son 18 las que deberian y CERO las que
llegan. El retraso del pipeline es mas grande que la ventana en la que el aviso serviria.

Y TAMPOCO HABIA A QUIEN AVISAR. Censados uno por uno los 8 casos de 30 dias en que nadie
le hablo al jugador ni toco el CRM: 7 entraron de madrugada y se atendieron entre las
06:00 y las 06:46 al abrir el turno --incluidos un retiro de $100 y un problema de
deposito-- y el octavo era un "gracias". Cero abandonos.

TRES COSAS QUE APRENDIMOS MIDIENDO, y que valen para cualquier alerta futura de este tipo:
  * SE MIRA A LA PERSONA, NO AL TICKET. El cliente escribe a varias lineas a la vez; un
    caso que parecia abandono tenia la nota "SE LO ATENDIO EN LA OTRA LINEA (YA ESTA
    CARGADA)" veinte minutos despues.
  * LA NOTA DEL CRM ES ATENCION. Cuando el ultimo mensaje es un "gracias", el operador
    cierra sin responder --y eso esta BIEN--. Mirar solo `messages.from_me` acusa a gente
    que hizo su trabajo: los 7 "nunca respondidos" de una semana tenian un `*resuelto*`
    entre 9 y 56 segundos despues.
  * `captured_at` ES EL RELOJ HONESTO. Simular con `created_at` miente a favor: mide
    cuando el cliente escribio, no cuando pudimos enterarnos.

EL MECANISMO DE ENVIO SALE DE `grafana-llm-alertas`, que ya manda a Telegram en
produccion. Se copio tal cual lo que alli ya esta probado:

  * un POST a `api.telegram.org/bot{token}/sendMessage`, `timeout=10`, que devuelve
    bool y NUNCA lanza. El scoring es el producto; la alerta es un aviso, y un aviso que
    revienta no puede tumbar el worker.
    CON `httpx` Y NO `requests`: alla usan `requests`, aca ya esta `httpx` (src/llm.py) y
    la forma de la llamada es la misma. Una alerta no justifica una dependencia nueva.
  * Si falta el token o el chat, el canal CALLA y el envio se saltea SIN error: sirve
    para desplegar en seco antes de conectar el grupo. Es textual de su `.env.example`.
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
from datetime import datetime, timedelta

import httpx

from src.horario import espera_efectiva

logger = logging.getLogger(__name__)

# ~10 msg/s. El limite del Bot API es mas alto; esto deja margen y ordena la salida.
THROTTLE_SEGUNDOS = 0.15

# LA VENTANA. El negocio lo dijo asi: "no me importan los de ayer, solo alertar a los de
# ahora". Un resumen viejo NO es noticia: la conversacion cerro, la nota esta puesta y el
# tablero la tiene. Dos horas alcanzan de sobra como red por si el worker se reinicio; mas
# que eso es replicar historia. (Era un DIA, y con el ledger vacio eso son 155 mensajes de
# golpe.)
VENTANA_RESUMEN_HORAS = 2

# CUANDO PASO LA CHARLA, que NO es lo mismo que cuando la calificamos. `scored_at` dice
# cuando la MIRAMOS: un backfill de scoring le pone `now()` a una conversacion de marzo.
# VISTO EN EL PRIMER ARRANQUE EN PRODUCCION: el worker sesionizo 144.594 sesiones y las va
# scoreando de a lotes; sin este filtro, cada lote habria mandado resumenes de charlas
# muertas. La siembra del primer barrido solo tapa el ciclo UNO.
# Es mas ancha que la del scoreo a proposito: una charla que cerro anoche y se califica a
# la mañana sigue siendo noticia.
VENTANA_CHARLA_HORAS = 24

# LA ESPERA LARGA, marcada en el resumen. Es la vara del negocio: 5 minutos.
#
# ES LA VERSION QUE SI SE PUEDE de "avisar que un VIP espero". La alerta EN VIVO se borro
# porque el pipeline llega tarde por aritmetica; esta es retrospectiva --la charla ya
# cerro-- asi que no necesita ganarle a nadie de mano y el numero esta MEDIDO, no inferido.
# No puede acusar a alguien que si atendio.
#
# CUANTO MARCA: 23 de 890 resumenes VIP (2,6%) superan los 5 minutos. Se destaca sin ahogar
# el canal; si marcara la mitad no distinguiria nada.
UMBRAL_ESPERA_LARGA_SEGUNDOS = 300
MARCA_ESPERA_LARGA = "⏳"

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
        -- `tipo` sobrevive a la baja de la alerta de latencia (2026-08-31) y hoy vale
        -- siempre 'resumen'. Se conserva porque es parte de la PK: sacarlo pide migrar
        -- una tabla viva para no ganar nada, y deja la puerta abierta a un tipo nuevo.
        tipo       text        NOT NULL,
        clave      text        NOT NULL,   -- que INTERACCION, no que jugador
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


def ledger_vacio(cur, account: str) -> bool:
    """No hay rastro de ninguna alerta de esta cuenta: es el PRIMER arranque."""
    cur.execute("SELECT count(*) FROM alertas_enviadas WHERE account = %s", (account,))
    fila = cur.fetchone()
    # Sin fila es lo mismo que sin rastro: se trata como primer arranque, que es el lado
    # seguro -- siembra y no manda.
    return not fila or fila[0] == 0


def clave_resumen(interaccion_id: str) -> str:
    """La INTERACCION. Un re-scoreo masivo --como el de v22-- no puede volver a avisar de
    una charla de hace un mes.

    ERA LA SESION hasta el grano interaccion (2026-08-27). Con una nota por interaccion,
    dedupear por sesion dejaria mudas a N-1 atenciones: el jefe de ATC veria una de cinco
    y no tendria como saber que faltan cuatro. Cada una es un operador distinto con su
    propia nota, asi que cada una es una alerta.
    """
    return str(interaccion_id)


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
    un desfase mueve la ventana de las dos puntas y el resumen se manda de mas o de
    menos. Un solo reloj y se acabo la clase de bug.
    """
    cur.execute("SELECT now()")
    return cur.fetchone()[0]


# --- de donde salen los candidatos ------------------------------------------
#
# LA CONSULTA VIVE ACA Y NO EN src/queries.py: aquel modulo es el del TABLERO y pasa por
# `_rows_as_dicts`, que censura. Esta alimenta un canal interno y devuelve metadatos, no
# texto de chat.

# EL RESUMEN SALE DE LA SESION YA SCOREADA, que es lo que define "termino la conversacion":
# `worker.fetch_pending_sessions` trae sesiones CERRADAS sin scorear, y cuando el score se
# persiste ya existen quien atendio, la nota, la duracion y el motivo. Antes de eso no hay
# resumen que mandar.
#
# EL LEFT JOIN CONTRA EL LEDGER es lo que evita repetir: el barrido corre cada 60 segundos.
# Y la ventana de un dia es la red por si el ledger se vacia -- sin ella, conectar el bot
# despues de un rescore masivo dispararia miles de mensajes de charlas viejas.
_RESUMEN_SQL = """
SELECT cs.interaccion_id::text AS interaccion_id,
       cs.conversation_id::text AS session_id,
       v.username, v.ranking, v.agencia,
       v.motivo         AS motivo_vip,
       cs.user_name     AS operador,
       cs.motivo        AS motivo,
       cs.stars, cs.first_response_seconds, cs.resolution_seconds,
       -- CUANDO ESCRIBIO EL CLIENTE. La necesita `espera_de_horario` para descontar la
       -- noche: `first_response_seconds` es reloj de pared y sin esta columna la marca
       -- de espera larga publicaria las horas que el negocio estuvo cerrado.
       cs.conversation_created_at AS conversation_created_at,
       cs.segment       AS segment,
       -- LA LLAVE PARA BUSCAR EN EL TABLERO. El buscador matchea `contacts.name`,
       -- `contacts.number` y el operador: por el `username` del CASINO no se puede
       -- buscar. Y la cuenta dice cual de los dos tableros abrir.
       ct.name          AS cliente,
       cs.account       AS account
FROM conversation_scores cs
JOIN tickets t      ON t.id = cs.ticket_id
LEFT JOIN contacts ct ON ct.id = t.contact_id
JOIN vip_players v ON v.contact_id = t.contact_id::text AND v.account = cs.account
LEFT JOIN alertas_enviadas a
       ON a.account = cs.account AND a.tipo = 'resumen'
      -- POR INTERACCION, no por conversacion: con N notas en una charla, dedupear por
      -- conversacion hace que la primera alerta tape a todas las demas.
      AND a.clave = cs.interaccion_id::text
WHERE v.es_vip
  AND cs.account = %(account)s
  AND a.clave IS NULL
  AND cs.eval_status = 'evaluated'
  AND cs.scored_at > now() - make_interval(hours => %(ventana_h)s)
  -- Y la CHARLA tiene que ser reciente: `scored_at` dice cuando la miramos, no cuando
  -- paso. Sin esto, un backfill de scoring alerta conversaciones de hace meses.
  AND coalesce(cs.resolved_at, cs.conversation_created_at)
        > now() - make_interval(hours => %(charla_h)s)
"""


def _dicts(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def resumenes_pendientes(cur, account: str) -> list[dict]:
    """Sesiones VIP ya calificadas que todavia no se avisaron."""
    cur.execute(_RESUMEN_SQL, {"account": account, "ventana_h": VENTANA_RESUMEN_HORAS,
                               "charla_h": VENTANA_CHARLA_HORAS})
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


def _buscar_en(d: dict) -> str:
    """La linea con que ABRIR el caso: el nombre entre comillas y la cuenta.

    ENTRECOMILLADO a proposito: es lo que se pega tal cual en el buscador del tablero. Un
    contacto sin nombre no imprime comillas vacias --no sirven para buscar nada-- pero la
    cuenta va igual, porque sin ella no se sabe cual de los dos tableros abrir.
    """
    cli = (d.get("cliente") or "").strip()
    nombre = f'"{_esc(cli)}" · ' if cli else ""
    return f"🔎 {nombre}cuenta {_esc(d.get('account') or _SIN_DATO)}"


def espera_de_horario(d: dict) -> tuple[float | None, bool]:
    """(segundos que espero DE HORARIO, si la noche recorto la cifra).

    POR QUE NO ALCANZA `first_response_seconds`: es RELOJ DE PARED. `metrics` lo calcula
    como una resta pelada y no descuenta la noche, asi que un mensaje que entra con el
    negocio cerrado acumula horas que nadie podia atender.

    EL CASO QUE OBLIGA A ESTO (`05123a61`, real): el cliente escribio 00:06 y la operadora
    contesto 06:05. Reloj de pared **359 minutos**; el negocio abre 06:00, asi que la
    espera de horario son **5,7 minutos**. Publicar "esperó 6 horas" en el grupo donde lee
    gerencia es acusar a quien contesto a los seis minutos de abrir el turno. De las 23
    esperas que el reloj de pared marca sobre 5 min, DOS son de esta clase.

    SE USA `horario.espera_efectiva` Y NO UNA RESTA PROPIA: es la MISMA funcion con la que
    la rubrica califica la agilidad. Una segunda version del contrato es como se rompen
    estas cosas -- el tablero diria una cosa y Telegram otra sobre el mismo hecho.

    SIN LA HORA EN QUE ESCRIBIO NO SE PUEDE DESCONTAR NADA, y ahi devuelve el reloj de
    pared SIN marcar como recortado... salvo que ni eso haya. Ante la duda no se acusa: el
    llamador solo marca cuando hay hora, porque marcar a ciegas es justo el falso positivo
    que este diseño viene a evitar.
    """
    bruto = d.get("first_response_seconds")
    desde = d.get("conversation_created_at")
    if bruto is None or desde is None:
        return bruto, False
    try:
        hasta = desde + timedelta(seconds=float(bruto))
    except (TypeError, ValueError):
        return bruto, False
    efectiva = espera_efectiva(desde, hasta)
    if efectiva is None:
        return bruto, False
    seg = efectiva.total_seconds()
    # El redondeo al segundo evita el "5 min contra 5 min" por milisegundos, la misma
    # leccion que ya esta en `horario.espera_efectiva`.
    return seg, round(seg) < round(float(bruto))


def espera_larga(d: dict) -> bool:
    """Se marca la espera larga? Pide DOS cosas, y la primera es la que protege.

    1. QUE SE PUEDA DESCONTAR LA NOCHE, o sea que haya `conversation_created_at`. Sin esa
       hora solo queda el reloj de pared, y marcar con el reloj de pared es exactamente el
       falso positivo que este diseño viene a evitar: acusaria de "6 horas" a quien
       contesto seis minutos despues de abrir. Ante la duda NO se acusa -- el resumen sale
       igual, sin la marca, y el numero sigue estando en la linea del reloj.
    2. Que la espera DE HORARIO supere la vara del negocio.
    """
    if d.get("conversation_created_at") is None:
        return False
    seg, _ = espera_de_horario(d)
    return seg is not None and seg >= UMBRAL_ESPERA_LARGA_SEGUNDOS


def _estrellas(v) -> str:
    """`★★★★☆`. Se lee de un vistazo; "4 de 5" hay que leerlo."""
    try:
        n = max(0, min(5, int(round(float(v)))))
    except (TypeError, ValueError):
        return "☆☆☆☆☆"
    return "★" * n + "☆" * (5 - n)


# QUE SE DICE CUANDO NO HAY `motivo`. No es un dato faltante: el motivo (deposito, retiro,
# registro) es de la rubrica del JUGADOR, y a un agente se lo juzga con otra. Poner "N/D"
# hace pensar en un bug -- se vio en una alerta real de produccion.
_PARA_QUE_SIN_MOTIVO = {"agente": "atención a agente", "interno": "consulta interna"}


def _para_que(d: dict) -> str:
    motivo = (d.get("motivo") or "").strip()
    if motivo:
        return _esc(motivo)
    return _esc(_PARA_QUE_SIN_MOTIVO.get(d.get("segment"), "sin clasificar"))


def mensaje_resumen(d: dict) -> str:
    """RESUMEN. Es un REGISTRO: se lee sin apuro y no pide hacer nada.

    LA NOTA BAJA SE MARCA PERO NO SE VUELVE ALERTA. Un resumen de 2 estrellas sigue siendo
    info --nadie tiene que correr-- pero es la info que alguien deberia mirar, asi que las
    estrellas la delatan de un vistazo sin cambiarle el tono al mensaje.
    """
    estrellas = d.get("stars")
    nota = f"{estrellas:g} de 5" if estrellas is not None else "sin nota"
    seg, recortada = espera_de_horario(d)
    # LA CIFRA QUE SE PUBLICA ES LA DE HORARIO, no la de reloj de pared. Ver
    # `espera_de_horario`. El sufijo aparece SOLO cuando las dos difieren: sin el, el que
    # compara con el tablero --que muestra el reloj de pared-- cree que una de las dos
    # miente; puesto siempre, es ruido en el 97% de los mensajes.
    tiempo = f"⏱ 1ª respuesta {duracion(seg)}"
    if recortada:
        tiempo += " de horario"
    tiempo += f" · duró {duracion(d.get('resolution_seconds'))}"
    cuerpo = [
        "🍀 <b>Conversación cerrada</b>",
    ]
    # LA MARCA VA ARRIBA Y NOMBRA A QUIEN RESPONDIO, que es lo que pidio el negocio: la
    # primera linea es lo que se lee en la notificacion sin abrir el chat.
    if espera_larga(d):
        cuerpo.append(f"{MARCA_ESPERA_LARGA} <b>Esperó {duracion(seg)} por respuesta</b>"
                      f" — {_quien(d.get('operador'))}")
    cuerpo += [
        "",
        f"👤 <b>{_esc(d.get('username') or _SIN_DATO)}</b>"
        f"  <code>#{_esc(d.get('ranking') or '?')}</code>  {_esc(d.get('agencia') or _SIN_DATO)}",
        _buscar_en(d),
        f"🧑‍💼 {_quien(d.get('operador'))} · 📌 {_para_que(d)}",
        f"{_estrellas(estrellas)}  {nota}",
        tiempo,
        f"⭐ VIP por {_esc(d.get('motivo_vip') or _SIN_DATO)}",
    ]
    return "\n".join(cuerpo)


# --- el barrido: lo unico que el worker necesita llamar ----------------------

def barrer(conn, account: str, canal: Canal,
           ahora: datetime | None = None,
           log=None, ledger_vacio_=None) -> dict:
    """Un ciclo de alertas de UNA cuenta. Devuelve `{"resumen": n, ...}`.

    EL ORDEN ES: marcar primero, mandar despues. Al reves, un fallo de red despues del
    envio dejaria la alerta sin rastro y el proximo barrido --sesenta segundos mas tarde--
    la repetiria. Se prefiere perder un aviso a repetirlo: el canal que dice dos veces lo
    mismo se deja de leer.

    NO LANZA NUNCA. El worker lo llama dentro de su ciclo de scoring; una alerta rota no
    puede dejar sesiones sin calificar.
    """
    # `fallos` NO es cosmetico. Antes se devolvia solo lo ENVIADO: si los diez envios del
    # ciclo fallaban, esto daba {resumen:0}, que es lo MISMO que devuelve un dia tranquilo.
    # El worker solo loguea cuando hay algo, asi que un canal caido se veia identico a que
    # no hubiera pasado nada.
    hecho = {"resumen": 0, "fallos": 0, "sembrados": 0}
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
            # EL PRIMER ARRANQUE NO REPLICA HISTORIA. El despliegue sube el codigo con el
            # token VACIO, asi que el ledger llega vacio al momento de encender, y la
            # consulta de resumen mira 24 h atras. MEDIDO: 0 en 24 h sobre la copia pero
            # **155 con ventana de 72 h** -- en produccion, poner el token dispararia el
            # backlog entero de un saque y el canal nace quemado. Un canal de alertas
            # arranca en AHORA: la primera pasada SIEMBRA el ledger sin mandar nada.
            primera = ledger_vacio(cur, account) if ledger_vacio_ is None else ledger_vacio_
        conn.commit()

        # NO MIRA EL HORARIO: una conversacion que cerro 23:50 se avisa igual. El resumen
        # no pide que nadie entre a atender, asi que llegar de noche no molesta.
        if canal.configurado:
            with conn.cursor() as cur:
                pendientes = resumenes_pendientes(cur, account)
            for r in pendientes:
                with conn.cursor() as cur:
                    nueva = marcar_enviada(cur, account, "resumen",
                                           clave_resumen(r["interaccion_id"]))
                conn.commit()
                if not nueva:
                    continue
                if primera:
                    hecho["sembrados"] += 1
                    continue
                estado = canal.enviar(mensaje_resumen(r))
                if estado == OK:
                    hecho["resumen"] += 1
                else:
                    hecho["fallos"] += 1
                    decir(f"[alertas] {account} resumen {r['interaccion_id']}: {estado}")
                if estado == REINTENTAR:
                    with conn.cursor() as cur:
                        desmarcar(cur, account, "resumen", clave_resumen(r["interaccion_id"]))
                    conn.commit()
                time.sleep(THROTTLE_SEGUNDOS)
        if primera and hecho["sembrados"]:
            decir(f"[alertas] {account}: PRIMER arranque, {hecho['sembrados']} del backlog "
                  f"quedan marcados SIN enviar. Desde el proximo ciclo se avisa lo nuevo.")
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
