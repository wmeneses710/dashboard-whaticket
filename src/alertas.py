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
from datetime import datetime, timedelta, timezone

import httpx

from src.cola_de_cortesia import decidir_con_el_modelo, necesita_el_modelo
from src.horario import TZ as TZ_EC
from src.horario import espera_efectiva
from src.interacciones import SILENCIO_MAX, partir_en_interacciones, tiempos_de
from src.signals import client_sin_motivo

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

# EL TOPE. Pasado esto no hubo una espera: hubo otra conversacion. CASO REAL
# (`carlosvilla0105`): "Muchas gracias" y la "respuesta" 1.105 minutos despues -- el jugador
# volvio a escribir al dia siguiente. Es `interacciones.SILENCIO_MAX`, el mismo umbral con
# el que se corta la interaccion, y se importa de alli para no tener dos definiciones.
TOPE_ESPERA_SEGUNDOS = SILENCIO_MAX.total_seconds()

# LA VENTANA DE LA CONSULTA, ancha A PROPOSITO. Es la leccion del 17,9%: `_RESUMEN_SQL` mira
# 2 h y el 2026-08-31 se midio que 15 de 84 resumenes NUNCA salieron, porque el ciclo del
# worker tardo entre 2,4 y 3,9 horas y la fila se cayo de la ventana antes de que el barrido
# pasara. Sin error y sin log. Lo que impide el duplicado es el LEDGER, no una ventana
# angosta; la ventana solo acota el scan.
VENTANA_ESPERA_LARGA_HORAS = 24

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


def ledger_vacio(cur, account: str, tipo: str) -> bool:
    """No hay rastro de ESTE TIPO de alerta en esta cuenta: es su PRIMER arranque.

    POR TIPO Y NO POR CUENTA, y la diferencia es el dia del despliegue. Mirando la cuenta,
    una alerta NUEVA encuentra el ledger lleno de otro tipo --207 resumenes-- se saltea la
    siembra y su primer barrido manda el backlog entero de la ventana. El negocio lo pidio
    al reves: "las alertas deben ser con lo que vayan llegando, no deben acumularse".
    """
    cur.execute("SELECT count(*) FROM alertas_enviadas WHERE account = %s AND tipo = %s",
                (account, tipo))
    fila = cur.fetchone()
    # Sin fila es lo mismo que sin rastro: se trata como primer arranque, que es el lado
    # seguro -- siembra y no manda.
    return not fila or fila[0] == 0


def clave_espera_larga(ticket_id: str, cliente_at: datetime) -> str:
    """El EPISODIO de espera, no el ticket.

    Si la clave fuera solo el ticket, el jugador que vuelve a esperar mañana en la misma
    conversacion no alertaria NUNCA otra vez. El instante del mensaje del cliente es lo que
    distingue una espera de la siguiente.
    """
    return f"{ticket_id}:{cliente_at.isoformat()}"


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

# LA ESPERA LARGA NO PASA POR `conversation_scores`, Y ESA ES LA GRACIA. Pedido del negocio:
# "no necesitamos todo para esta alerta... solo necesitamos el inicio y la primera respuesta
# en si, no es necesario mandar la calificacion". Los dos datos viven en `messages`, asi que
# esta alerta NO espera a que la sesion sea elegible ni a que el worker llegue a calificarla.
# MEDIDO sobre 30 dias, cuanto tarda en poder saberse:
#     por el resumen ...... p50 122 min (sesion de 1 interaccion) a 61 h (sesion de 20+),
#                           y ademas el 17,9% no sale nunca
#     por `messages` ...... p50 2,9 min desde que el ETL captura la RESPUESTA (p90 57 min)
#
# EL PAR ES (mensaje del cliente -> mensaje del negocio que le sigue). `LEAD` sobre la
# ventana del ticket: se alerta solo cuando la respuesta YA EXISTE, asi que el hecho esta
# cerrado y no se acusa a nadie de estar haciendo esperar a alguien ahora mismo.
#
# LAS NOTAS QUEDAN AFUERA (`is_note`). Una nota es interna: contarla como respuesta al
# cliente es el bug que `sin_respuesta.hubo_respuesta_del_negocio` ya documenta.
_ESPERA_LARGA_SQL = """
SELECT t.id::text  AS ticket_id,
       m.created_at, m.from_me, m.body, m.media_type,
       coalesce(m.is_note, false) AS is_note,
       -- QUIEN ESCRIBIO ESTE MENSAJE, que no es lo mismo que quien tiene el ticket
       -- asignado hoy. `messages.user_id` viene en 1.725 de los 1.728 mensajes del
       -- negocio de 7 dias y es la unica fuente que describe el MENSAJE, no el envase.
       -- (SIN el signo de porcentaje en el comentario: psycopg parsea el SQL entero
       -- buscando placeholders y uno suelto revienta con 'incomplete placeholder'.)
       autor.name AS autor,
       v.username, v.ranking, v.agencia,
       v.motivo    AS motivo_vip,
       usr.name    AS operador,
       ct.name     AS cliente,
       t.account   AS account
  FROM messages m
  JOIN tickets t     ON t.id = m.ticket_id
  JOIN vip_players v ON v.contact_id = t.contact_id::text AND v.account = t.account
  LEFT JOIN users usr   ON usr.id = t.user_id
  LEFT JOIN users autor ON autor.id = m.user_id
  LEFT JOIN contacts ct ON ct.id = t.contact_id
 WHERE v.es_vip
   AND t.account = %(account)s
   AND NOT coalesce(t.is_group, false)
   AND m.created_at > now() - make_interval(hours => %(lookback_h)s)
   -- Solo los tickets que se movieron en la ventana. Sin esto se arrastra el historial de
   -- cada VIP para nada.
   AND EXISTS (SELECT 1 FROM messages m2
                WHERE m2.ticket_id = t.id
                  AND m2.created_at > now() - make_interval(hours => %(ventana_h)s))
 ORDER BY t.id, m.created_at
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
       cs.account       AS account,
       -- PARA QUEDARSE CON LA DE RECIEN. Sin estas dos, dos interacciones del mismo
       -- ticket calificadas en el mismo lote salen como dos mensajes seguidos.
       t.id::text       AS ticket_id,
       cs.interaccion_fin AS interaccion_fin
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


_CAMPOS_DEL_TICKET = ("ticket_id", "username", "ranking", "agencia", "motivo_vip",
                      "operador", "cliente", "account")


def candidatos_espera_larga(cur, account: str, ahora: datetime | None = None) -> list[dict]:
    """Un candidato por INTERACCION abierta por el cliente: su arranque y su 1ª respuesta.

    EL CORTE SE HACE EN PYTHON, no en SQL, y no es capricho: la unidad la define
    `interacciones.partir_en_interacciones`, que lee las notas del CRM (`*resuelto*`,
    `*reabierto*`), el silencio de 6 h y la gracia de los 120 s del adjunto. Reescribir eso
    en SQL seria una segunda version del contrato con el que la rubrica califica.

    SE TRAEN MAS MENSAJES QUE LA VENTANA (`lookback`) para que el corte tenga CONTEXTO: una
    interaccion que arranco hace 26 h y sigue viva se partiria mal si el transcript empieza
    justo en el borde. Los candidatos si se acotan a la ventana.
    """
    cur.execute(_ESPERA_LARGA_SQL, {
        "account": account,
        "ventana_h": VENTANA_ESPERA_LARGA_HORAS,
        # ENTERO: `make_interval(hours => ...)` no acepta un double precision, y esta
        # division da float. Lo encontro la prueba contra la base, no los tests.
        "lookback_h": VENTANA_ESPERA_LARGA_HORAS + int(SILENCIO_MAX.total_seconds() // 3600),
    })
    filas = _dicts(cur)
    por_ticket: dict[str, tuple[dict, list[dict]]] = {}
    for f in filas:
        tk = f["ticket_id"]
        if tk not in por_ticket:
            por_ticket[tk] = ({k: f.get(k) for k in _CAMPOS_DEL_TICKET}, [])
        por_ticket[tk][1].append(f)
    corte = (ahora or datetime.now(tz=TZ_EC)) - timedelta(hours=VENTANA_ESPERA_LARGA_HORAS)
    out = []
    for ticket, mensajes in por_ticket.values():
        for c in esperas_de_apertura(mensajes, ticket):
            if c["cliente_at"] >= corte:
                out.append(c)
    return out


_EPOCA = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _que_tan_reciente(d: dict):
    """Clave de orden TOTAL: sin fin cuenta como la mas vieja, y el id desempata.

    El desempate no es cosmetico: sin el, dos barridos que reciben las mismas filas en
    distinto orden silencian interacciones distintas, y una alerta se pierde de a ratos.
    """
    fin = d.get("interaccion_fin")
    return (fin is not None, fin or _EPOCA, str(d.get("interaccion_id")))


def solo_la_ultima_por_ticket(pendientes: list[dict]) -> tuple[list[dict], list[dict]]:
    """`(las que se mandan, las que se callan)`. Una alerta por ticket: la de recien.

    PEDIDO DEL NEGOCIO, textual: *"debe ser alerta de la interaccion, no me interesa la
    anterior, quiero que me alerte de la de recien"*.

    POR QUE HACE FALTA. Hay una fila POR INTERACCION y el scoring viene atrasado, asi que
    dos interacciones del mismo ticket nacen calificadas en el MISMO lote. MEDIDO sobre las
    195 alertas del ledger: de 194 pares consecutivos, **126 llegan a menos de 2 minutos y
    100 de esas son del MISMO TICKET**. No es un defecto del envio --el loop dispara entre
    0,4 y 1,3 min de que existe la nota-- sino de que un VIP se mueve todo el dia.

    LO QUE CUESTA, y se decidio pagarlo: de las 103 alertas que esto suprime, **17 eran de
    2 estrellas o menos**. El negocio: *"que igual luego hay que revisar esas 2*, porque no
    todas estan bien clasificadas"*.

    SIN `ticket_id` NO SE AGRUPA. Ante la duda no se calla a nadie: perder un aviso por una
    columna vacia es peor que mandar uno de mas.
    """
    por_ticket: dict = {}
    for d in pendientes:
        t = d.get("ticket_id")
        if t is not None:
            por_ticket.setdefault(t, []).append(d)
    ganan = {str(max(fs, key=_que_tan_reciente).get("interaccion_id"))
             for fs in por_ticket.values() if len(fs) > 1}
    hermanas = {t for t, fs in por_ticket.items() if len(fs) > 1}
    manda, calla = [], []
    for d in pendientes:
        sola = d.get("ticket_id") not in hermanas
        (manda if sola or str(d.get("interaccion_id")) in ganan else calla).append(d)
    return manda, calla


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


def esperas_de_apertura(mensajes: list[dict], ticket: dict) -> list[dict]:
    """Un candidato POR INTERACCION: (arranque del cliente, primera respuesta del operador).

    CORRECCION DEL NEGOCIO (2026-08-31), y cambia la unidad de medida: *"si se guarda un
    mensaje de una conversacion iniciada no [alerta] porque no tiene nada que ver, ya es el
    inicio y la primera respuesta de una interaccion lo que importa"*.

    LA PRIMERA VERSION MEDIA CUALQUIER TURNO y por eso alertaba, sobre datos reales, por
    "ID: 5336766639", "Me confirma" y "Qué el juego quedó 68 a 68": mensajes del MEDIO de
    una charla donde el operador ya estaba adentro. Eso no es esperar por atencion, es el
    ida y vuelta normal -- y avisarlo es acusar a alguien que ya atendio.

    SE CORTA CON `interacciones.partir_en_interacciones` Y SE LEE CON `tiempos_de`, que son
    las MISMAS funciones con las que la rubrica califica agilidad. Ese corte sabe cosas que
    una consulta SQL no puede saber: `*resuelto*` cierra, `*reabierto*` delata el cierre que
    no pego, el silencio de 6 h parte, el adjunto de los 120 s sigue siendo el mismo gesto,
    y la cola de cortesia se pega a la atencion que la gano.

    LA INTERACCION QUE ABRE EL NEGOCIO NO ES UNA ESPERA. Si el primer mensaje real es del
    operador (una campaña, un seguimiento) no habia nadie esperando y el reloj no aplica.

    `respuesta_at = None` significa "en ESTE ticket todavia nadie contesto", y OJO CON
    LEERLO COMO ABANDONO. El corte es por TICKET, y el jugador escribe a varias lineas: una
    respuesta que cayo en otro ticket suyo no se ve desde aca. VERIFICADO sobre 7 dias, las
    2 interacciones sin respuesta que parecian pedidos reales estaban las dos atendidas --a
    una le contesto Andree en otro ticket 1 h 51 min despues, y la otra tenia la nota
    "ESCRIBIO Y SE LO ATENDIO EN LA OTRA LINEA (YA ESTA CARGADA)"--. Las otras 5 eran
    "gracias" y stickers que el operador cerro sin responder, que es lo correcto.

    POR ESO NO SE ALERTA CON `None`, y es la proteccion, no una omision: `merece_alerta_de_
    espera` exige las dos puntas. Medir una espera que no termino es la alerta EN VIVO que
    se borro por imposible, y afirmar abandono mirando un solo ticket es el falso positivo
    que ya nos comimos una vez.
    """
    out = []
    for frag in partir_en_interacciones(mensajes):
        reales = [m for m in frag if not m.get("is_note")]
        if not reales or reales[0].get("from_me"):
            continue
        inicio, primera_op, _ = tiempos_de(frag)
        if inicio is None:
            continue
        # QUIEN CONTESTO ES EL AUTOR DE ESE MENSAJE, no el asignado al ticket. El bug lo
        # destapo leer los 15 transcripts de la evaluacion: `tickets.user_id` daba OTRA
        # persona en 11 de 15 (decia "Andree" y habia contestado Alejandra; decia "Alex" y
        # fue Salome Ramirez). Equivocarse en la PERSONA, en el grupo donde lee gerencia,
        # es el peor falso positivo que puede tener este canal.
        # Se busca sobre `reales`: la nota `*Asignado automaticamente* a X` la escribe el
        # CRM y nombrar a X seria cobrarle una respuesta que no dio.
        autor = next((m.get("autor") for m in reales
                      if m.get("from_me") and m["created_at"] == primera_op), None)
        # TODO LO QUE EL CLIENTE DIJO MIENTRAS ESPERABA, no solo con lo que abrio. El tramo
        # se corta en la PRIMERA RESPUESTA: lo que diga despues ya no es espera, y dejarlo
        # entrar convertiria cualquier charla larga en una alerta.
        hasta = primera_op or reales[-1]["created_at"]
        del_cliente = [{"from_me": False, "is_note": False,
                        "body": m.get("body") or "",
                        "media_type": m.get("media_type") or "chat"}
                       for m in reales
                       if not m.get("from_me") and m["created_at"] <= hasta]
        out.append({**ticket,
                    "cliente_at": inicio,
                    "respuesta_at": primera_op,
                    # Sin `messages.user_id` (3 de 1.728) se cae al del ticket: quedarse
                    # mudo es peor que dar el mejor nombre que tenemos.
                    "operador": autor or ticket.get("operador"),
                    "cliente_msgs": del_cliente,
                    "body": reales[0].get("body") or "",
                    "media_type": reales[0].get("media_type") or "chat"})
    return out


def merece_alerta_de_espera(d: dict, llm=None) -> bool:
    """El VIP espero de mas por su PRIMERA respuesta, y valia la pena esperarla.

    TRES COMPUERTAS, y el orden importa porque las dos primeras son GRATIS:

    1. LAS DOS PUNTAS. Sin la hora del cliente o la de la respuesta no hay espera que
       medir, y medir a medias es inventar.
    2. EL RELOJ, con `horario.espera_efectiva` -- la MISMA funcion con la que la rubrica
       califica agilidad, no una resta propia. Entre el umbral y `TOPE_ESPERA_SEGUNDOS`:
       por debajo no es larga, por encima no es una espera sino otra conversacion.
    3. QUE EL CLIENTE HUBIERA PEDIDO ALGO **EN TODO LO QUE DIJO MIENTRAS ESPERABA**,
       no solo con lo que abrio. Mirar el primer mensaje se comia el peor caso de los
       7 dias: el VIP #2 abrio con "Buenas", siguió con un reclamo de una apuesta mas
       comprobante, insistio dos veces y espero 1 h 18 min -- y la alerta no salia.
       Dos capas, como en v24:
         capa 1  `signals.client_sin_motivo`, determinista y gratis. Sobre los 121
                 candidatos de 30 dias mata 32 ("Ok", "Gracias", "😑", "Entiendo").
         capa 2  el modelo, SOLO sobre el residuo que `necesita_el_modelo` deja pasar. Es
                 lo que caza "Bueno mi bro gracias 🫂" y "Bueno bro", que la capa 1 no ve.
                 Costo medido en v24: 0,8 inferencias por dia.

    LA POLARIDAD DEL RIESGO, y aca es al reves que en el scoring. Alla ante la duda se
    PUNTUA para no perder un reclamo; aca ante la duda se ALERTA, porque el negocio eligio
    priorizar al VIP con el numero a la vista (88 alertas en 30 dias a 5 minutos, contra 8
    a 15). Un fallo del modelo --sin LLM, timeout, JSON roto-- devuelve None y se alerta.

    UN MEDIA SUELTO ALERTA. Un audio o un comprobante sin texto no se puede leer, pero de
    un VIP es casi siempre un pedido; `client_sin_motivo` ya trata el adjunto como un
    planteo y `necesita_el_modelo` no gasta inferencia en el.
    """
    desde, hasta = d.get("cliente_at"), d.get("respuesta_at")
    if desde is None or hasta is None:
        return False
    msg = d.get("cliente_msgs") or [
        {"from_me": False, "is_note": False, "body": d.get("body") or "",
         "media_type": d.get("media_type") or "chat"}]
    efectiva = espera_efectiva(desde, hasta)
    if efectiva is None:
        return False
    seg = efectiva.total_seconds()
    if not (UMBRAL_ESPERA_LARGA_SEGUNDOS <= seg <= TOPE_ESPERA_SEGUNDOS):
        return False
    if client_sin_motivo(msg):
        return False
    if llm is not None and necesita_el_modelo(msg):
        # True = el modelo dice que es cortesia. `False` y `None` alertan igual, pero se
        # distinguen a proposito: uno es una decision y el otro un fallo que hay que poder
        # contar (misma regla que `cola_de_cortesia.decidir_con_el_modelo`).
        if decidir_con_el_modelo(msg, d.get("ultimo_del_negocio"), llm) is True:
            return False
    return True


def segundos_de_espera(d: dict) -> float | None:
    """La espera DE HORARIO entre el mensaje del cliente y su primera respuesta."""
    efectiva = espera_efectiva(d.get("cliente_at"), d.get("respuesta_at"))
    return None if efectiva is None else efectiva.total_seconds()


def mensaje_espera_larga(d: dict) -> str:
    """La alerta de espera larga. Lleva CUANTO espero y QUIEN respondio, que es lo que
    pidio el negocio, y las dos horas para que se pueda auditar sin abrir el chat.

    SIN EL TEXTO DEL CHAT, igual que el resumen: el cuerpo llevaria cedulas y numeros de
    cuenta a un grupo de Telegram donde nadie los tapa.
    """
    return "\n".join([
        f"{MARCA_ESPERA_LARGA} <b>VIP esperó {duracion(segundos_de_espera(d))}"
        f" por respuesta</b>",
        "",
        f"👤 <b>{_esc(d.get('username') or _SIN_DATO)}</b>"
        f"  <code>#{_esc(d.get('ranking') or '?')}</code>  {_esc(d.get('agencia') or _SIN_DATO)}",
        _buscar_en(d),
        f"🧑‍💼 respondió {_quien(d.get('operador'))}",
        f"🕐 Escribió {_hora_ec(d.get('cliente_at'))}"
        f" · le respondieron {_hora_ec(d.get('respuesta_at'))}",
        f"⭐ VIP por {_esc(d.get('motivo_vip') or _SIN_DATO)}",
    ])


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


def _hora_ec(cuando) -> str:
    """La hora local de Ecuador, que es donde trabaja el que lee la alerta."""
    if not isinstance(cuando, datetime):
        return _SIN_DATO
    return cuando.astimezone(TZ_EC).strftime("%H:%M")


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
           log=None, ledger_vacio_=None, llm=None) -> dict:
    """Un ciclo de alertas de UNA cuenta. Devuelve `{"espera_larga": n, "resumen": n, ...}`.

    `llm` es OPCIONAL y solo lo usa la capa 2 de la espera larga. Sin el, la compuerta
    de cortesia se queda con la capa determinista: alerta de mas, nunca de menos.

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
    hecho = {"espera_larga": 0, "resumen": 0, "fallos": 0, "sembrados": 0,
             "silenciadas": 0}
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
            # UNA POR TIPO: cada alerta arranca en AHORA por su cuenta.
            primera = {t: (ledger_vacio(cur, account, t) if ledger_vacio_ is None
                           else ledger_vacio_)
                       for t in ("espera_larga", "resumen")}
        conn.commit()

        # ESPERA LARGA. Va PRIMERO: es la unica de las dos que alguien puede querer accionar
        # el mismo dia, y no depende del scoring, asi que sale aunque el worker este
        # arrastrando backlog.
        if canal.configurado:
            with conn.cursor() as cur:
                candidatos = candidatos_espera_larga(cur, account, ahora)
            for c in candidatos:
                if not merece_alerta_de_espera(c, llm):
                    continue
                clave = clave_espera_larga(c["ticket_id"], c["cliente_at"])
                with conn.cursor() as cur:
                    nueva = marcar_enviada(cur, account, "espera_larga", clave)
                conn.commit()
                if not nueva:
                    continue
                if primera["espera_larga"]:
                    hecho["sembrados"] += 1
                    continue
                estado = canal.enviar(mensaje_espera_larga(c))
                if estado == OK:
                    hecho["espera_larga"] += 1
                else:
                    hecho["fallos"] += 1
                    decir(f"[alertas] {account} espera_larga {clave}: {estado}")
                if estado == REINTENTAR:
                    with conn.cursor() as cur:
                        desmarcar(cur, account, "espera_larga", clave)
                    conn.commit()
                time.sleep(THROTTLE_SEGUNDOS)

        # NO MIRA EL HORARIO: una conversacion que cerro 23:50 se avisa igual. El resumen
        # no pide que nadie entre a atender, asi que llegar de noche no molesta.
        if canal.configurado:
            with conn.cursor() as cur:
                pendientes = resumenes_pendientes(cur, account)
            pendientes, calladas = solo_la_ultima_por_ticket(pendientes)
            # LAS CALLADAS SE MARCAN IGUAL. Silenciar sin marcar las devuelve al proximo
            # barrido y salen solas sesenta segundos despues: el mismo ruido, con retraso.
            silencio = {str(c["interaccion_id"]) for c in calladas}
            for r in pendientes + calladas:
                with conn.cursor() as cur:
                    nueva = marcar_enviada(cur, account, "resumen",
                                           clave_resumen(r["interaccion_id"]))
                conn.commit()
                if not nueva:
                    continue
                if str(r["interaccion_id"]) in silencio:
                    hecho["silenciadas"] += 1
                    continue
                if primera["resumen"]:
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
        if any(primera.values()) and hecho["sembrados"]:
            sembrados = [t for t, v in primera.items() if v]
            decir(f"[alertas] {account}: PRIMER arranque de {'/'.join(sembrados)}, "
                  f"{hecho['sembrados']} del backlog quedan marcados SIN enviar. "
                  f"Desde el proximo ciclo se avisa lo nuevo.")
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
