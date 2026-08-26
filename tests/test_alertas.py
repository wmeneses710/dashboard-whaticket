"""Tests de src/alertas.py: las dos alertas de jugador VIP.

LAS DOS, Y SE DISPARAN POR MOTIVOS DISTINTOS (dictado por el negocio el 2026-08-26):
  ESPERA   el cliente lleva mas de 5 minutos sin respuesta, EN HORARIO de atencion.
  RESUMEN  termino una conversacion suya: quien la atendio, para que, la calificacion,
           la duracion y el motivo.

LO QUE SE COPIO DE grafana-llm-alertas, que ya manda a Telegram en produccion:
  * un POST a `api.telegram.org/bot{token}/sendMessage`, timeout 10, que devuelve bool y
    NUNCA lanza (con `httpx`, que el repo ya tiene, en vez de `requests`): una alerta que se cae no puede tumbar el worker de scoring.
  * Si falta el token o el chat, el canal no manda nada y el envio se saltea SIN error --
    sirve para desplegar en seco. UN SOLO bot: alla usan uno por tipo de alerta, aca el
    negocio pidio un unico canal y las dos se distinguen por el titulo (⏳ / 🍀).
  * Throttle de 0,15 s entre envios (~10 msg/s, lejos del limite del Bot API), sin dormir
    en el primero.

LO QUE **NO** SE COPIO, y es la diferencia de fondo: alla el disparo es ENTRANTE (Grafana
hace POST). Aca nadie nos avisa: el disparo es nuestro, desde el worker. Por eso la
idempotencia no puede depender de que el emisor no reintente -- la ponemos nosotros, con
`alertas_enviadas`. Es el mismo problema que alla resolvieron desacoplando en un hilo
porque "Grafana corta por timeout, reintenta el POST, y salen mensajes duplicados".
"""
from datetime import datetime, timedelta, timezone

from src import alertas

TZ = timezone(timedelta(hours=-5))       # Ecuador, sin horario de verano


_COLS_RESUMEN = ("session_id", "username", "ranking", "agencia", "motivo_vip",
                 "operador", "motivo", "stars", "first_response_seconds",
                 "resolution_seconds")


class _FakeCursor:
    def __init__(self, rows=None, cols=_COLS_RESUMEN):
        self.sql, self.params, self._rows = [], [], rows or []
        self.rowcount = 0
        self.description = [type("D", (), {"name": c}) for c in cols]

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(params)
        self.rowcount = len(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# --- el canal ---------------------------------------------------------------

def test_un_canal_SIN_configurar_no_manda_y_no_es_un_error():
    """Desplegar en seco: si falta el token o el chat, ese canal calla. Es lo que hace
    grafana-llm-alertas y el motivo esta escrito en su .env.example."""
    for token, chat in (("", "123"), ("abc", ""), ("", "")):
        c = alertas.Canal(token=token, chat_id=chat)
        assert not c.configurado
        assert c.enviar("hola") == alertas.DESCARTAR


def test_el_canal_configurado_pega_a_la_api_de_telegram(monkeypatch):
    visto = {}

    def _post(url, json=None, timeout=None):
        visto.update(url=url, json=json, timeout=timeout)
        return type("R", (), {"status_code": 200, "text": "ok"})()

    monkeypatch.setattr(alertas.httpx, "post", _post)
    assert alertas.Canal(token="TOK", chat_id="42").enviar("hola") == alertas.OK
    assert visto["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert visto["json"]["chat_id"] == "42" and visto["json"]["text"] == "hola"
    assert visto["json"]["parse_mode"] == "HTML"
    assert visto["timeout"] == 10


def test_si_telegram_falla_devuelve_False_y_NO_lanza(monkeypatch):
    """Una alerta que revienta no puede tumbar el worker de scoring: el scoring es el
    producto, la alerta es un aviso."""
    def _explota(*a, **k):
        raise OSError("sin red")

    monkeypatch.setattr(alertas.httpx, "post", _explota)
    assert alertas.Canal(token="T", chat_id="1").enviar("x") == alertas.REINTENTAR

    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 429, "text": "slow"})())
    assert alertas.Canal(token="T", chat_id="1").enviar("x") == alertas.REINTENTAR


def test_el_throttle_no_duerme_en_el_PRIMER_envio(monkeypatch):
    """0,15 s entre mensajes (~10/s), pero el primero sale ya. Con 20 alertas la diferencia
    es de 3 s de arranque."""
    dormidas = []
    monkeypatch.setattr(alertas.time, "sleep", dormidas.append)
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    c = alertas.Canal(token="T", chat_id="1")
    assert alertas.enviar_lote(c, ["a", "b", "c"]) == 3
    assert dormidas == [alertas.THROTTLE_SEGUNDOS, alertas.THROTTLE_SEGUNDOS]


def test_un_lote_a_un_canal_apagado_no_duerme_ni_manda(monkeypatch):
    dormidas = []
    monkeypatch.setattr(alertas.time, "sleep", dormidas.append)
    assert alertas.enviar_lote(alertas.Canal("", ""), ["a", "b"]) == 0
    assert dormidas == []


# --- la idempotencia, que aca la ponemos NOSOTROS ---------------------------

def test_la_alerta_ya_enviada_no_se_manda_de_nuevo():
    """Sin esto, el barrido del worker manda la misma alerta cada 60 segundos."""
    cur = _FakeCursor()
    alertas.marcar_enviada(cur, "sistemas", "espera", "tkt-1:2026-08-26T14:32:00")
    junto = " ".join(cur.sql)
    assert "INSERT INTO alertas_enviadas" in junto
    assert "ON CONFLICT" in junto and "DO NOTHING" in junto


def test_la_clave_de_ESPERA_lleva_el_instante_del_mensaje():
    """Si la clave fuera solo el ticket, un cliente que vuelve a esperar MAÑANA no volveria
    a alertar nunca. El episodio es (ticket, ultimo mensaje del cliente)."""
    t1 = datetime(2026, 8, 26, 14, 32, tzinfo=TZ)
    t2 = datetime(2026, 8, 27, 9, 5, tzinfo=TZ)
    assert alertas.clave_espera("tkt-1", t1) != alertas.clave_espera("tkt-1", t2)
    assert alertas.clave_espera("tkt-1", t1) == alertas.clave_espera("tkt-1", t1)


def test_la_clave_de_RESUMEN_es_la_sesion():
    """Una sesion se scorea una vez; si se re-scorea (un rescore masivo, como el de v22) no
    puede volver a avisar de una charla de hace un mes."""
    assert alertas.clave_resumen("sess-9") == alertas.clave_resumen("sess-9")
    assert alertas.clave_resumen("sess-9") != alertas.clave_resumen("sess-8")


# --- ESPERA: el horario manda -----------------------------------------------

def _cand(minutos_atras=10, **kw):
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    d = {"ticket_id": "tkt-1", "contact_id": "c1", "ultimo_cliente_at": ahora - timedelta(minutes=minutos_atras),
         "queue": "Soporte", "operador": "Andree"}
    d.update(kw)
    return d


def test_no_alerta_por_debajo_del_umbral():
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    assert alertas.filtrar_espera([_cand(3)], ahora, umbral_segundos=300) == []


def test_alerta_por_encima_del_umbral():
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    out = alertas.filtrar_espera([_cand(9)], ahora, umbral_segundos=300)
    assert len(out) == 1 and out[0]["espera_segundos"] >= 300


def test_FUERA_de_horario_no_alerta_nunca():
    """A las 04:00 no hay nadie trabajando. Es la misma regla que ya evita que el tablero
    reproche 'respondió 1,7 horas después' cuando fueron 8 minutos (src/horario.py)."""
    madrugada = datetime(2026, 8, 26, 4, 0, tzinfo=TZ)
    assert alertas.filtrar_espera([_cand(120)], madrugada, umbral_segundos=300) == []


def test_la_espera_se_mide_EFECTIVA_y_no_por_reloj_de_pared():
    """Un cliente que escribio 23:50 y sigue esperando a las 06:05 NO lleva 6 h 15 min
    esperando: lleva 15 minutos de horario (10 antes de cerrar + 5 desde que abrio).

    Sin esto, cada mañana al abrir dispararia una tormenta de alertas anunciando esperas
    de horas que en realidad fue la noche. Es lo mismo que el tablero ya no reprocha."""
    anoche = datetime(2026, 8, 25, 23, 50, tzinfo=TZ)
    manana = datetime(2026, 8, 26, 6, 5, tzinfo=TZ)
    out = alertas.filtrar_espera([_cand(ultimo_cliente_at=anoche)], manana, umbral_segundos=300)
    assert len(out) == 1, "15 minutos efectivos SI pasan el umbral de 5"
    assert out[0]["espera_segundos"] == 15 * 60
    pared = (manana - anoche).total_seconds()
    assert pared > 6 * 3600, "el reloj de pared diria 6 h 15 min"
    assert out[0]["espera_segundos"] < pared / 20


def test_una_espera_que_arranca_ANTES_de_abrir_no_infla():
    """El cliente escribio a las 03:00 y son las 06:10: lleva 10 minutos, no 3 horas."""
    madrugada = datetime(2026, 8, 26, 3, 0, tzinfo=TZ)
    abierto = datetime(2026, 8, 26, 6, 10, tzinfo=TZ)
    out = alertas.filtrar_espera([_cand(ultimo_cliente_at=madrugada)], abierto,
                                 umbral_segundos=300)
    assert len(out) == 1 and out[0]["espera_segundos"] == 10 * 60


# --- los mensajes -----------------------------------------------------------

def test_el_mensaje_de_RESUMEN_lleva_las_cinco_cosas_que_pidio_el_negocio():
    txt = alertas.mensaje_resumen({
        "username": "quirozsabando", "ranking": 1, "agencia": "ModoSorti",
        "motivo_vip": "GGRx5", "operador": "Andree", "motivo": "deposito",
        "stars": 4, "first_response_seconds": 72, "resolution_seconds": 480})
    assert "quirozsabando" in txt
    assert "Andree" in txt                    # quien le atendio
    assert "deposito" in txt.lower()          # para que
    assert "4" in txt                         # la calificacion
    assert "8 min" in txt                     # la duracion
    assert "GGRx5" in txt                     # por que es VIP


def test_el_mensaje_de_ESPERA_dice_CUANTO_lleva():
    txt = alertas.mensaje_espera({
        "username": "quirozsabando", "ranking": 1, "agencia": "ModoSorti",
        "motivo_vip": "GGRx5", "operador": "Andree", "queue": "Soporte",
        "espera_segundos": 437})
    assert "quirozsabando" in txt and "7 min" in txt
    assert "Andree" in txt and "Soporte" in txt


def test_el_mensaje_NO_lleva_el_texto_del_chat():
    """La alerta viaja a un grupo de Telegram. El METADATO alcanza para decidir si alguien
    tiene que entrar; el cuerpo del mensaje llevaria cedulas, cuentas y credenciales --lo
    que src/censura.py tapa en el tablero-- a un canal donde nadie lo tapa."""
    campos = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M",
              "operador": "O", "motivo": "deposito", "stars": 4,
              "first_response_seconds": 10, "resolution_seconds": 20,
              "queue": "Q", "espera_segundos": 400, "body": "mi cedula es 1712345678"}
    for txt in (alertas.mensaje_resumen(campos), alertas.mensaje_espera(campos)):
        assert "1712345678" not in txt and "cedula" not in txt.lower()


def test_el_operador_sin_asignar_no_imprime_None():
    txt = alertas.mensaje_espera({"username": "u", "ranking": None, "agencia": "A",
                                  "motivo_vip": "M", "operador": None, "queue": None,
                                  "espera_segundos": 400})
    assert "None" not in txt


def test_la_duracion_se_escribe_en_UN_solo_lugar():
    """La leccion de formatting.py en grafana-llm-alertas: el formato repartido en cada
    f-string divergio y el mismo mensaje imprimia dos notaciones. Y el singular de un
    minuto es el mismo bug que ya arreglamos en el tablero el 2026-08-25."""
    assert alertas.duracion(45) == "45 s"
    assert alertas.duracion(60) == "1 min"
    assert alertas.duracion(72) == "1 min"
    assert alertas.duracion(480) == "8 min"
    assert alertas.duracion(3600) == "1 h"
    assert alertas.duracion(5400) == "1 h 30 min"
    assert alertas.duracion(None) == "N/D"
    # EL DIA. Con la ventana de 2 dias el tope real son ~36 h de horario, pero "2496 h"
    # salio en una prueba y no se lee. Un tramo largo se cuenta en dias, como lo diria
    # una persona.
    assert alertas.duracion(86400) == "1 d"
    assert alertas.duracion(129600) == "1 d 12 h"
    assert alertas.duracion(172800) == "2 d"


# --- el barrido, que es lo que llama el worker ------------------------------

class _ConnFalsa:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        conn = self

        class _Ctx:
            def __enter__(self_inner):
                return conn._cur

            def __exit__(self_inner, *a):
                return False
        return _Ctx()

    def commit(self):
        self.commits += 1


def test_el_barrido_no_manda_nada_si_el_canal_esta_apagado(monkeypatch):
    """Desplegar en seco: se puede subir el worker con las alertas escritas y sin bot, y no
    pasa nada. Es lo que permite probar el resto antes de crear el grupo."""
    llamadas = []
    monkeypatch.setattr(alertas.httpx, "post", lambda *a, **k: llamadas.append(a))
    cur = _FakeCursor()
    conn = _ConnFalsa(cur)
    r = alertas.barrer(conn, "sistemas", alertas.Canal("", ""),
                       ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ))
    assert r == {"espera": 0, "resumen": 0, "fallos": 0, "sembrados": 0}
    assert llamadas == [], "un canal apagado no tiene que pegarle a la red"
    assert not any("INSERT INTO alertas_enviadas" in s for s in cur.sql), \
        "y tampoco puede marcar como enviada una alerta que nunca salio"


def test_el_barrido_FUERA_de_horario_ni_consulta_la_espera(monkeypatch):
    """A las 04:00 no hay nadie: la compuerta va ANTES de la consulta, no despues. Un
    barrido cada 60 s durante las 6 horas de cierre son 360 consultas para nada."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cur = _FakeCursor()
    alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                   ahora=datetime(2026, 8, 26, 4, 0, tzinfo=TZ))
    assert not any("WITH ultimo AS" in s for s in cur.sql)


def test_UN_solo_canal_para_las_dos_alertas():
    """El negocio pidio un unico bot. Las dos alertas se distinguen por el titulo, no por
    el destino, asi que el prefijo tiene que dejar claro cual es de un vistazo."""
    env = {"TELEGRAM_TOKEN_VIP": "T", "TELEGRAM_CHAT_VIP": "-100"}
    c = alertas.canal_desde_env(env)
    assert c.configurado and c.token == "T" and c.chat_id == "-100"
    assert not alertas.canal_desde_env({}).configurado
    base = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M", "operador": "O",
            "motivo": "deposito", "stars": 4, "first_response_seconds": 10,
            "resolution_seconds": 20, "queue": "Q", "espera_segundos": 400}
    assert alertas.mensaje_espera(base).startswith("\U0001f6a8"), "la alerta grita"
    assert alertas.mensaje_resumen(base).startswith("\U0001f340"), "el resumen informa"


# --- EL BUG QUE APARECIO PROBANDO CONTRA EL BOT DE VERDAD --------------------
#
# CASO REAL (2026-08-26, bot @vipplayers_bot, grupo VIP_atc_sorti). Con `parse_mode`
# Markdown, este mensaje devolvio **400 Bad Request: can't parse entities**:
#
#     🍀 *VIP atendido* — *andrea_deniss* #7 · OnlySorti
#     Para qué: soporte_cuenta
#
# Dos guiones bajos en datos --el username y el motivo-- y el parser de Telegram intenta
# abrir una cursiva. HOY son 4 de 155 alertas posibles (3%), pero `soporte_cuenta` y
# `sin_motivo` son motivos corrientes y cualquier username nuevo con `_` entra al club.
#
# LO GRAVE NO ERA EL 400: `barrer` MARCA ANTES DE MANDAR, asi que la alerta quedaba
# registrada como enviada y no llegaba NUNCA. El compromiso "prefiero perder un aviso a
# repetirlo" es correcto para un corte de red, y era veneno para un error deterministico
# que va a fallar siempre igual.

def test_el_mensaje_va_en_HTML_y_no_en_Markdown(monkeypatch):
    """HTML tiene TRES caracteres que escapar (& < >) y Markdown dieciocho. Con datos que
    vienen del CRM y del casino, el que se puede garantizar es HTML."""
    visto = {}
    monkeypatch.setattr(alertas.httpx, "post", lambda url, json=None, timeout=None: (
        visto.update(json), type("R", (), {"status_code": 200, "text": ""})())[1])
    alertas.Canal("T", "1").enviar("hola")
    assert visto["parse_mode"] == "HTML"


def test_el_guion_bajo_del_username_y_del_motivo_ya_no_rompe():
    """El caso exacto que devolvio 400 contra el bot real."""
    txt = alertas.mensaje_resumen({
        "username": "andrea_deniss", "ranking": 7, "agencia": "OnlySorti",
        "motivo_vip": "R90", "operador": "Andree", "motivo": "soporte_cuenta",
        "stars": 2, "first_response_seconds": 400, "resolution_seconds": 1200})
    assert "andrea_deniss" in txt and "soporte_cuenta" in txt
    assert "<b>" in txt, "el enfasis va con etiquetas, no con asteriscos"
    assert "*" not in txt, "un asterisco suelto es lo que rompia"


def test_los_TRES_caracteres_de_HTML_se_escapan():
    """Un nombre de cola o de operador con `&` o `<` viene del CRM: no lo controlamos."""
    txt = alertas.mensaje_espera({"username": "a<b>c", "ranking": 1, "agencia": "A & B",
                                  "motivo_vip": "M", "operador": "O'Brien & hijo",
                                  "queue": "Soporte <1>", "espera_segundos": 400})
    assert "&lt;b&gt;" in txt and "&amp;" in txt
    assert "<b>" in txt, "el formato NUESTRO sigue siendo etiqueta de verdad"


# --- perder un aviso por un BUG no es lo mismo que perderlo por la red ------

def test_un_400_NO_se_reintenta_pero_un_corte_de_red_SI(monkeypatch):
    """Un 400 va a fallar igual la proxima vez: reintentarlo es ruido. Un 429 o un corte
    de red es transitorio y la alerta todavia sirve."""
    def _con(status):
        return lambda *a, **k: type("R", (), {"status_code": status, "text": "x"})()
    c = alertas.Canal("T", "1")
    monkeypatch.setattr(alertas.httpx, "post", _con(400))
    assert c.enviar("x") == alertas.DESCARTAR
    for transitorio in (429, 500, 502):
        monkeypatch.setattr(alertas.httpx, "post", _con(transitorio))
        assert c.enviar("x") == alertas.REINTENTAR, transitorio


def test_si_el_envio_es_TRANSITORIO_se_DESMARCA_para_que_reintente(monkeypatch):
    """Marcar antes de mandar protege del duplicado; sin desmarcar, un corte de red de un
    segundo borra la alerta del mundo."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 500, "text": ""})())
    cur = _FakeCursor(rows=[("s1",)])
    alertas.desmarcar(cur, "sistemas", "resumen", "s1")
    assert any("DELETE FROM alertas_enviadas" in s for s in cur.sql)


# --- LA CONFIRMACION EN DOS OBSERVACIONES -----------------------------------
#
# Idea del negocio: no alertar en la primera vez que vemos la espera. Anotar el episodio,
# esperar la tanda siguiente, y recien alertar si SIGUE sin respuesta.
#
# POR QUE HACE FALTA: el ETL no entrega en orden. MEDIDO sobre 20 dias, el 71,6% de los
# mensajes llega DESPUES de otro mas nuevo, y cuando llega tarde esta p50 3 min y p90 2 h
# atras. O sea: que no veamos la respuesta no prueba que no exista.
#
# CUANTO BAJA, con honestidad: las falsas alarmas pasan de 2,06% a 1,71% de las
# conversaciones bien atendidas. Es poco, y aun asi va: es gratis, y una alerta que se
# puede defender vale mas que una que acierta un poco mas seguido.

def test_la_PRIMERA_vez_que_vemos_la_espera_NO_alerta(monkeypatch):
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cur = _FakeCursor()          # ledger vacio: es la primera observacion
    c = _cand(30)
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    assert alertas.confirmados(cur, "sistemas", [dict(c, espera_segundos=1800)], ahora) == []
    assert any("INSERT INTO alertas_enviadas" in s for s in cur.sql), \
        "la primera vez se ANOTA, para poder confirmarla despues"
    assert "espera_vista" in str(cur.params)


def test_la_SEGUNDA_vez_SI_alerta_si_sigue_sin_respuesta():
    """El episodio ya estaba anotado y la prorroga vencio: el silencio es real."""
    visto = datetime(2026, 8, 26, 13, 55, tzinfo=TZ)
    cur = _FakeCursor(rows=[(alertas.clave_espera("tkt-1", _cand()["ultimo_cliente_at"]), visto)])
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    out = alertas.confirmados(cur, "sistemas", [dict(_cand(), espera_segundos=1800)], ahora)
    assert len(out) == 1


def test_la_prorroga_tiene_que_VENCER_no_alcanza_con_estar_anotado():
    """Dos barridos separados por 3 segundos no son dos observaciones utiles: entre medio
    no entro ni un lote del ETL."""
    visto = datetime(2026, 8, 26, 13, 59, 57, tzinfo=TZ)
    cur = _FakeCursor(rows=[(alertas.clave_espera("tkt-1", _cand()["ultimo_cliente_at"]), visto)])
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    assert alertas.confirmados(cur, "sistemas", [dict(_cand(), espera_segundos=1800)], ahora) == []


def test_si_la_respuesta_aparece_el_episodio_simplemente_DEJA_de_ser_candidato():
    """No hace falta retractar nada: `candidatos_espera` deja de traerlo y la anotacion
    queda huerfana. Es la forma barata de cancelar."""
    cur = _FakeCursor(rows=[("otra-clave", datetime(2026, 8, 26, 13, 0, tzinfo=TZ))])
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    assert alertas.confirmados(cur, "sistemas", [], ahora) == []


# --- EL RELOJ: UNO SOLO, EL DE LA BASE --------------------------------------

def test_el_ahora_sale_de_la_BASE_y_no_del_reloj_de_la_app():
    """`barrer` comparaba `datetime.now()` de la APP contra timestamps de la BASE. Hoy dan
    igual porque la BD corre en 127.0.0.1; en produccion esta en otra maquina. Si la app
    atrasa, las esperas se acortan y la alerta no suena; si adelanta, se inflan."""
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    cur = _FakeCursor(rows=[(ahora,)])
    assert alertas.ahora_de_la_base(cur) == ahora
    assert "SELECT now()" in " ".join(cur.sql)


# --- el diseño que eligio el negocio: alerta B, resumen C -------------------

def test_la_ALERTA_grita_y_el_RESUMEN_informa():
    """Son dos cosas distintas y tienen que leerse distinto de un vistazo: una pide entrar
    ahora, la otra es un registro."""
    a = alertas.mensaje_espera({"username": "u", "ranking": 1, "agencia": "A",
                                "motivo_vip": "M", "operador": "O", "queue": "Q",
                                "espera_segundos": 400,
                                "ultimo_cliente_at": datetime(2026, 8, 26, 14, 0, tzinfo=TZ)})
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "deposito",
                                 "stars": 5, "first_response_seconds": 27,
                                 "resolution_seconds": 120})
    assert a.startswith("\U0001f6a8") and "ATENCIÓN" in a
    assert r.startswith("\U0001f340") and "cerrada" in r.lower()
    assert "★★★★★" in r, "la nota se lee de un vistazo, no contando digitos"


def test_la_alerta_dice_A_QUE_HORA_escribio_el_cliente():
    """MEDIDO: el 42,6% de las alertas puede dispararse tarde por el retraso del ETL --p75
    39 min, p90 2 h--. Si el mensaje solo dice "lleva 20 min", el que lo lee asume que es
    en vivo. La hora del cliente lo desmiente sin que nadie tenga que saber esto."""
    a = alertas.mensaje_espera({"username": "u", "ranking": 1, "agencia": "A",
                                "motivo_vip": "M", "operador": "O", "queue": "Q",
                                "espera_segundos": 1200,
                                "ultimo_cliente_at": datetime(2026, 8, 26, 14, 3, tzinfo=TZ)})
    assert "14:03" in a


def test_el_resumen_de_nota_BAJA_se_distingue():
    """Un resumen es info, pero uno de 2 estrellas es la info que alguien tiene que mirar.
    Se marca SIN convertirlo en alerta: sigue en el mismo tono."""
    base = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M",
            "operador": "O", "motivo": "deposito", "first_response_seconds": 27,
            "resolution_seconds": 120}
    bueno = alertas.mensaje_resumen({**base, "stars": 5})
    malo = alertas.mensaje_resumen({**base, "stars": 2})
    assert "★★☆☆☆" in malo and "★★★★★" in bueno
    assert malo != bueno and malo.startswith("\U0001f340"), "marcado, pero sigue siendo resumen"


# --- LA TRAMPA DEL DESPLIEGUE -----------------------------------------------

def test_el_barrido_asegura_TAMBIEN_la_tabla_de_VIP():
    """En una base NUEVA, `vip_players` no existe hasta que alguien corre el loader. Pero
    las dos consultas del barrido la usan: sin ella la consulta revienta, `barrer` se traga
    la excepcion y las alertas no funcionan NUNCA, en silencio y cada 60 segundos.

    Asegurarla desde el barrido vuelve el despliegue independiente del orden: una tabla
    vacia significa "no hay VIP que vigilar", que es lo correcto, no un error."""
    cur = _FakeCursor()
    alertas.ensure_table(cur)
    junto = " ".join(cur.sql)
    assert "CREATE TABLE IF NOT EXISTS alertas_enviadas" in junto
    assert "CREATE TABLE IF NOT EXISTS vip_players" in junto


# --- QUE SE VEA CUANDO FALLA ------------------------------------------------
#
# EL AGUJERO: `barrer` devolvia solo lo ENVIADO. Si los diez envios del ciclo fallaban,
# devolvia {espera:0, resumen:0} -- indistinguible de "no habia nada que avisar"-- y el
# worker, que solo loguea cuando hay algo, no escribia una linea. Un canal caido se veria
# exactamente igual que un dia tranquilo.

def test_barrer_reporta_los_FALLOS_no_solo_los_envios(monkeypatch):
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 500, "text": "boom"})())
    monkeypatch.setattr(alertas.time, "sleep", lambda s: None)
    cur = _FakeCursor(rows=[("s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
    cur.rowcount = 1
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ))
    assert "fallos" in r, "un ciclo con todo caido tiene que poder distinguirse de uno vacio"


def test_barrer_acepta_un_log_para_hablar_por_el_mismo_canal_que_el_worker(monkeypatch):
    """El worker escribe con timestamp por `emit`; `logger` de la libreria sale por stderr
    sin hora ni nombre. En un log de contenedor mezclado con uvicorn, `telegram 400` suelto
    no se puede rastrear. Con un `log` inyectado, la falla aparece en la misma corriente."""
    dichos = []
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 400, "text": "mal"})())
    monkeypatch.setattr(alertas.time, "sleep", lambda s: None)
    cur = _FakeCursor(rows=[("s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
    cur.rowcount = 1
    alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                   ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ), log=dichos.append)
    assert any("400" in d or "descarta" in d.lower() for d in dichos), dichos


# --- EL PRIMER ARRANQUE NO REPLICA HISTORIA ---------------------------------
#
# El despliegue va: subir el codigo con el token VACIO, y encender despues. Con el canal
# apagado `barrer` no marca nada, asi que el ledger llega VACIO al momento de encender --
# y la consulta de resumen mira 24 h hacia atras. Medido sobre la copia: 0 en 24 h pero
# **155 con ventana de 72 h**. En produccion, donde el scoring corre todo el dia, poner el
# token dispararia el backlog entero de un saque.
#
# Un canal de alertas arranca en AHORA, no replica lo que ya paso. La primera pasada
# SIEMBRA el ledger sin mandar nada; desde la segunda, solo lo nuevo.

def test_el_PRIMER_barrido_siembra_el_ledger_y_NO_manda(monkeypatch):
    enviados = []
    monkeypatch.setattr(alertas.httpx, "post", lambda *a, **k: (
        enviados.append(1), type("R", (), {"status_code": 200, "text": ""})())[1])
    monkeypatch.setattr(alertas.time, "sleep", lambda s: None)
    cur = _FakeCursor(rows=[("s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
    cur.rowcount = 1
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ), ledger_vacio_=True)
    assert enviados == [], "el backlog no se replica al encender"
    assert r["sembrados"] == 1
    assert "INSERT INTO alertas_enviadas" in " ".join(cur.sql), "pero SI queda marcado"


def test_saber_si_el_ledger_esta_vacio():
    cur = _FakeCursor(rows=[(0,)])
    assert alertas.ledger_vacio(cur, "sistemas") is True
    cur2 = _FakeCursor(rows=[(31,)])
    assert alertas.ledger_vacio(cur2, "sistemas") is False


# --- SOLO LO DE AHORA -------------------------------------------------------

def test_las_dos_ventanas_son_constantes_con_nombre():
    """El negocio lo dijo claro: "no me importan los de ayer, solo alertar a los de ahora".
    Las ventanas no pueden estar enterradas en un string de SQL."""
    assert alertas.VENTANA_RESUMEN_HORAS <= 4, "un resumen viejo ya no es noticia"
    # `make_interval` y no `interval '...'`: el parametro dentro de un literal SQL no se
    # sustituye de forma confiable y quedaria un intervalo roto en runtime.
    for sql in (alertas._RESUMEN_SQL, alertas._ESPERA_SQL):
        assert "make_interval(hours => %(ventana_h)s)" in sql


def test_la_ventana_de_ESPERA_es_mas_larga_a_proposito():
    """Un resumen viejo no es noticia; una ESPERA vieja sigue abierta -- el cliente todavia
    esta sin respuesta. Son preguntas distintas y por eso los numeros son distintos."""
    assert alertas.VENTANA_ESPERA_HORAS > alertas.VENTANA_RESUMEN_HORAS


# --- LA LLAVE PARA BUSCAR EN EL TABLERO -------------------------------------
#
# El `username` es del CASINO y en el tablero NO se puede buscar por el: el buscador
# matchea `contacts.name`, `contacts.number` y el nombre del operador ("cliente u
# operador…"). Sin el nombre del contacto, el que lee la alerta sabe QUIEN es pero no
# tiene con que abrirlo. Y sin la cuenta no sabe en cual de los dos tableros mirar.

def test_la_ALERTA_trae_con_que_buscar_en_el_tablero():
    a = alertas.mensaje_espera({"username": "quirozsabando", "ranking": 1, "agencia": "A",
                                "motivo_vip": "M", "operador": "O", "queue": "Q",
                                "espera_segundos": 400, "cliente": "Juan Pérez",
                                "account": "sistemas"})
    assert '"Juan Pérez"' in a, "entrecomillado: es lo que se pega en el buscador"
    assert "sistemas" in a


def test_el_RESUMEN_tambien():
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "deposito",
                                 "stars": 4, "first_response_seconds": 10,
                                 "resolution_seconds": 20, "cliente": "Ana Gómez",
                                 "account": "datos"})
    assert '"Ana Gómez"' in r and "datos" in r


def test_un_contacto_SIN_nombre_no_imprime_comillas_vacias():
    """Hay contactos sin `name`. Una linea con `""` no sirve para buscar nada."""
    for f in (alertas.mensaje_espera, alertas.mensaje_resumen):
        t = f({"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M",
               "operador": "O", "motivo": "d", "stars": 3, "espera_segundos": 400,
               "first_response_seconds": 1, "resolution_seconds": 2,
               "cliente": None, "account": "sistemas"})
        assert '""' not in t and "None" not in t
        assert "sistemas" in t, "la cuenta va igual: sin ella no se sabe que tablero abrir"


def test_el_nombre_del_cliente_se_ESCAPA():
    """Viene del CRM: lo escribe quien quiera."""
    t = alertas.mensaje_espera({"username": "u", "ranking": 1, "agencia": "A",
                                "motivo_vip": "M", "operador": "O", "queue": "Q",
                                "espera_segundos": 400, "cliente": "A & <b>B</b>",
                                "account": "sistemas"})
    assert "&amp;" in t and "&lt;b&gt;" in t


def test_las_consultas_TRAEN_el_nombre_y_la_cuenta():
    for sql in (alertas._ESPERA_SQL, alertas._RESUMEN_SQL):
        assert "AS cliente" in sql and "AS account" in sql


# --- EL RESUMEN NO PUEDE ALERTAR UNA CHARLA VIEJA RECIEN CALIFICADA ----------
#
# CASO REAL, visto en el log del primer arranque en produccion: el worker sesionizo
# **144.594 sesiones** y las va a ir scoreando. Cada una que califique se lleva
# `scored_at = now()`, asi que pasaba la ventana y disparaba un resumen -- aunque la
# conversacion fuera de marzo. La siembra del primer barrido solo tapa el ciclo UNO; del
# segundo en adelante, cada lote del backlog habria mandado alertas de charlas muertas.
#
# `scored_at` dice cuando la MIRAMOS. Lo que importa es cuando PASO.

def test_el_resumen_filtra_por_cuando_paso_la_conversacion():
    assert "cs.scored_at" in alertas._RESUMEN_SQL, "sigue mirando lo recien calificado"
    assert "coalesce(cs.resolved_at, cs.conversation_created_at)" in alertas._RESUMEN_SQL, \
        "pero la conversacion TAMBIEN tiene que ser reciente"


def test_la_ventana_de_la_conversacion_es_mas_ancha_que_la_del_scoreo():
    """El scoreo puede tardar: una charla que cerro anoche y se califica a la mañana sigue
    siendo noticia. Lo que no puede pasar es que una de marzo cuente como de hoy."""
    assert alertas.VENTANA_CHARLA_HORAS > alertas.VENTANA_RESUMEN_HORAS
    assert alertas.VENTANA_CHARLA_HORAS <= 48
