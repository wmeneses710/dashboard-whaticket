"""Tests de src/alertas.py: la alerta de jugador VIP.

QUE PIDIO EL NEGOCIO (2026-08-26):
  RESUMEN  termino una conversacion suya: quien la atendio, para que, la calificacion,
           la duracion y el motivo.

ERAN DOS. La de ESPERA se borro el 2026-08-31 contra 30 dias de datos: 132 esperas
superaban el umbral de 5 min del negocio y solo 3 habrian llegado. El detalle y el
guard que impide que vuelva estan al final de este archivo.

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


# `interaccion_id` va PRIMERO porque asi lo devuelve `_RESUMEN_SQL` desde el grano
# interaccion (2026-08-27): es la clave del ledger, y sin ella el barrido revienta con
# KeyError -- que es exactamente como se detecto que faltaba en estos fixtures.
_COLS_RESUMEN = ("interaccion_id", "session_id", "username", "ranking", "agencia", "motivo_vip",
                 "operador", "motivo", "stars", "first_response_seconds",
                 "resolution_seconds")


class _FakeCursor:
    """Un cursor falso que responde a UNA consulta.

    `para` existe porque el barrido corre DOS consultas con formas distintas (la de
    espera larga trae MENSAJES, la de resumen trae filas ya calificadas) y un cursor que
    devuelve las mismas filas para las dos hace estallar la que no le corresponde con un
    KeyError que no tiene nada que ver con lo que el test mide.
    """

    def __init__(self, rows=None, cols=_COLS_RESUMEN, para="resumen"):
        self.sql, self.params, self._todas = [], [], rows or []
        self._rows = self._todas
        self.rowcount = 0
        self.para = para
        self.description = [type("D", (), {"name": c}) for c in cols]

    def execute(self, sql, params=None):
        texto = " ".join(str(sql).split())
        self.sql.append(texto)
        self.params.append(params)
        if "FROM users u WHERE u.name" in texto:
            self._rows = self._todas if self.para == "firmas" else []
        elif "FROM conversation_scores cs" in texto:
            self._rows = self._todas if self.para == "resumen" else []
        elif "FROM messages m" in texto:
            self._rows = self._todas if self.para == "espera_larga" else []
        else:
            self._rows = self._todas
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
    alertas.marcar_enviada(cur, "sistemas", "resumen", "a1b2c3d4")
    junto = " ".join(cur.sql)
    assert "INSERT INTO alertas_enviadas" in junto
    assert "ON CONFLICT" in junto and "DO NOTHING" in junto


def test_la_clave_de_RESUMEN_es_la_INTERACCION():
    """La intencion original se conserva ENTERA: si se re-scorea (un rescore masivo, como el
    de v22) no puede volver a avisar de una charla de hace un mes. Lo que cambia es el GRANO.

    Con el grano interaccion (2026-08-27) una sesion tiene N notas, cada una de un operador
    distinto. Con la clave por SESION solo la primera avisaria y las otras N-1 quedarian
    mudas: el jefe de ATC veria una atencion de cinco y no sabria que faltan cuatro."""
    assert alertas.clave_resumen("int-9") == alertas.clave_resumen("int-9")
    assert alertas.clave_resumen("int-9") != alertas.clave_resumen("int-8")


def test_el_resumen_avisa_UNA_VEZ_POR_INTERACCION_y_no_por_sesion():
    """Dos atenciones de la misma sesion son dos alertas: son dos operadores y dos notas."""
    assert "cs.interaccion_id" in alertas._RESUMEN_SQL, (
        "el resumen sigue trayendo la clave por sesion: N-1 atenciones VIP no avisarian"
    )
    ledger = alertas._RESUMEN_SQL.split("LEFT JOIN alertas_enviadas", 1)[-1]
    assert "cs.interaccion_id::text" in ledger, (
        "el ledger dedupea por conversacion: la primera alerta tapa a todas las de esa charla"
    )


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


def test_el_mensaje_NO_lleva_el_texto_del_chat():
    """La alerta viaja a un grupo de Telegram. El METADATO alcanza para decidir si alguien
    tiene que entrar; el cuerpo del mensaje llevaria cedulas, cuentas y credenciales --lo
    que src/censura.py tapa en el tablero-- a un canal donde nadie lo tapa."""
    campos = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M",
              "operador": "O", "motivo": "deposito", "stars": 4,
              "first_response_seconds": 10, "resolution_seconds": 20,
              "queue": "Q", "body": "mi cedula es 1712345678"}
    txt = alertas.mensaje_resumen(campos)
    assert "1712345678" not in txt and "cedula" not in txt.lower()


def test_el_operador_sin_asignar_no_imprime_None():
    txt = alertas.mensaje_resumen({"username": "u", "ranking": None, "agencia": "A",
                                   "motivo_vip": "M", "operador": None, "motivo": "deposito",
                                   "stars": 4, "first_response_seconds": 10,
                                   "resolution_seconds": 20})
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
    assert r == {"espera_larga": 0, "resumen": 0, "fallos": 0, "sembrados": 0}
    assert llamadas == [], "un canal apagado no tiene que pegarle a la red"
    assert not any("INSERT INTO alertas_enviadas" in s for s in cur.sql), \
        "y tampoco puede marcar como enviada una alerta que nunca salio"


def test_UN_solo_canal():
    """El negocio pidio un unico bot, y el prefijo tiene que decir de un vistazo que es
    esto. Quedo UNA sola alerta (la de espera se borro el 2026-08-31), pero el titulo
    sigue importando: el grupo recibe tambien mensajes de personas."""
    env = {"TELEGRAM_TOKEN_VIP": "T", "TELEGRAM_CHAT_VIP": "-100"}
    c = alertas.canal_desde_env(env)
    assert c.configurado and c.token == "T" and c.chat_id == "-100"
    assert not alertas.canal_desde_env({}).configurado
    base = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M", "operador": "O",
            "motivo": "deposito", "stars": 4, "first_response_seconds": 10,
            "resolution_seconds": 20, "queue": "Q"}
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
    txt = alertas.mensaje_resumen({"username": "a<b>c", "ranking": 1, "agencia": "A & B",
                                   "motivo_vip": "M", "operador": "O'Brien & hijo",
                                   "motivo": "soporte <1>", "stars": 4,
                                   "first_response_seconds": 10, "resolution_seconds": 20})
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


# --- EL RELOJ: UNO SOLO, EL DE LA BASE --------------------------------------

def test_el_ahora_sale_de_la_BASE_y_no_del_reloj_de_la_app():
    """`barrer` comparaba `datetime.now()` de la APP contra timestamps de la BASE. Hoy dan
    igual porque la BD corre en 127.0.0.1; en produccion esta en otra maquina. Si la app
    atrasa o adelanta, la ventana del resumen se corre y se manda de mas o de menos."""
    ahora = datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
    cur = _FakeCursor(rows=[(ahora,)])
    assert alertas.ahora_de_la_base(cur) == ahora
    assert "SELECT now()" in " ".join(cur.sql)


# --- el diseño que eligio el negocio: el resumen INFORMA, no pide entrar ----

def test_el_RESUMEN_informa_y_la_nota_se_lee_de_un_vistazo():
    """Es un registro, no un pedido de auxilio: abre con 🍀 y las estrellas se ven sin
    contar digitos."""
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "deposito",
                                 "stars": 5, "first_response_seconds": 27,
                                 "resolution_seconds": 120})
    assert r.startswith("\U0001f340") and "cerrada" in r.lower()
    assert "★★★★★" in r, "la nota se lee de un vistazo, no contando digitos"


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
# devolvia {resumen:0} -- indistinguible de "no habia nada que avisar"-- y el
# worker, que solo loguea cuando hay algo, no escribia una linea. Un canal caido se veria
# exactamente igual que un dia tranquilo.

def test_barrer_reporta_los_FALLOS_no_solo_los_envios(monkeypatch):
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 500, "text": "boom"})())
    monkeypatch.setattr(alertas.time, "sleep", lambda s: None)
    cur = _FakeCursor(rows=[("i1", "s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
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
    cur = _FakeCursor(rows=[("i1", "s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
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
    cur = _FakeCursor(rows=[("i1", "s1", "u", "1", "A", "M", "Op", "deposito", 4, 10, 20)])
    cur.rowcount = 1
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ), ledger_vacio_=True)
    assert enviados == [], "el backlog no se replica al encender"
    assert r["sembrados"] == 1
    assert "INSERT INTO alertas_enviadas" in " ".join(cur.sql), "pero SI queda marcado"


def test_saber_si_el_ledger_esta_vacio():
    cur = _FakeCursor(rows=[(0,)])
    assert alertas.ledger_vacio(cur, "sistemas", "resumen") is True
    cur2 = _FakeCursor(rows=[(31,)])
    assert alertas.ledger_vacio(cur2, "sistemas", "resumen") is False


# --- SOLO LO DE AHORA -------------------------------------------------------

def test_la_ventana_es_una_constante_con_nombre():
    """El negocio lo dijo claro: "no me importan los de ayer, solo alertar a los de ahora".
    La ventana no puede estar enterrada en un string de SQL."""
    assert alertas.VENTANA_RESUMEN_HORAS <= 4, "un resumen viejo ya no es noticia"
    # `make_interval` y no `interval '...'`: el parametro dentro de un literal SQL no se
    # sustituye de forma confiable y quedaria un intervalo roto en runtime.
    assert "make_interval(hours => %(ventana_h)s)" in alertas._RESUMEN_SQL


# --- LA LLAVE PARA BUSCAR EN EL TABLERO -------------------------------------
#
# El `username` es del CASINO y en el tablero NO se puede buscar por el: el buscador
# matchea `contacts.name`, `contacts.number` y el nombre del operador ("cliente u
# operador…"). Sin el nombre del contacto, el que lee la alerta sabe QUIEN es pero no
# tiene con que abrirlo. Y sin la cuenta no sabe en cual de los dos tableros mirar.

def test_el_RESUMEN_trae_con_que_buscar_en_el_tablero():
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "deposito",
                                 "stars": 4, "first_response_seconds": 10,
                                 "resolution_seconds": 20, "cliente": "Ana Gómez",
                                 "account": "datos"})
    assert '"Ana Gómez"' in r and "datos" in r


def test_un_contacto_SIN_nombre_no_imprime_comillas_vacias():
    """Hay contactos sin `name`. Una linea con `""` no sirve para buscar nada."""
    t = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "d",
                                 "stars": 3, "first_response_seconds": 1,
                                 "resolution_seconds": 2,
                                 "cliente": None, "account": "sistemas"})
    assert '""' not in t and "None" not in t
    assert "sistemas" in t, "la cuenta va igual: sin ella no se sabe que tablero abrir"


def test_el_nombre_del_cliente_se_ESCAPA():
    """Viene del CRM: lo escribe quien quiera."""
    t = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "d",
                                 "stars": 3, "first_response_seconds": 1,
                                 "resolution_seconds": 2, "cliente": "A & <b>B</b>",
                                 "account": "sistemas"})
    assert "&amp;" in t and "&lt;b&gt;" in t


def test_la_consulta_TRAE_el_nombre_y_la_cuenta():
    assert "AS cliente" in alertas._RESUMEN_SQL
    assert "AS account" in alertas._RESUMEN_SQL


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


# --- EL `N/D` DEL MOTIVO ----------------------------------------------------
#
# VISTO EN UNA ALERTA REAL de produccion: `📌 N/D`. El 10,2% de las sesiones evaluadas no
# tiene `motivo`, y **185 de esas 299 son del segmento `agente`**: el motivo (deposito,
# retiro, registro) es de la rubrica del JUGADOR, y a un agente se lo juzga con otra. No
# es un dato faltante, es una pregunta que no aplica -- y "N/D" hace pensar en un bug.

def test_sin_motivo_se_dice_el_SEGMENTO_en_vez_de_N_D():
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": None,
                                 "segment": "agente", "stars": 4, "account": "sistemas",
                                 "first_response_seconds": 10, "resolution_seconds": 20})
    assert "N/D" not in r and "atención a agente" in r


def test_un_jugador_sin_motivo_dice_sin_clasificar():
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": None,
                                 "segment": "jugador", "stars": 4, "account": "sistemas",
                                 "first_response_seconds": 10, "resolution_seconds": 20})
    assert "sin clasificar" in r and "N/D" not in r


def test_con_motivo_manda_el_motivo():
    r = alertas.mensaje_resumen({"username": "u", "ranking": 1, "agencia": "A",
                                 "motivo_vip": "M", "operador": "O", "motivo": "deposito",
                                 "segment": "agente", "stars": 4,
                                 "first_response_seconds": 10, "resolution_seconds": 20})
    assert "deposito" in r and "agente" not in r


def test_la_consulta_trae_el_segmento():
    assert "cs.segment" in alertas._RESUMEN_SQL


# --- LA ESPERA SE BORRO (2026-08-31) ----------------------------------------
#
# POR QUE. Medido sobre 30 dias de la copia, en horario de atencion y con el umbral de 5
# minutos que pidio el negocio: 132 esperas superaron el umbral y solo 3 habrian llegado.
#
#   100 de 132  el operador contesto ANTES de que la alerta pudiera existir (espera p50
#               6,6 min contra un piso de 10 min = 5 de umbral + 5 de prorroga)
#    29 de 132  el ETL nos entrego el mensaje del cliente cuando ya estaba atendida
#               (p50 de captura 26 min)
#     3 de 132  llegaban, y solo porque el ETL ese dia tardo 0,4 min
#
# Y de las que llegaban, NINGUNA era un abandono real: censados los supervivientes uno por
# uno, 7 de 8 entraron de madrugada y se atendieron entre las 06:00 y las 06:46 al abrir el
# turno, y el octavo era un "gracias".
#
# NO ES UN BUG QUE SE ARREGLE SUBIENDO O BAJANDO EL UMBRAL: a 30 minutos la cuenta da 18
# que deberian y CERO que llegan. El retraso del pipeline es mayor que la ventana en la que
# la alerta serviria para algo.
#
# LO QUE SE CONSERVA es el RESUMEN, que si funciona (207 enviados) y no depende de llegar a
# tiempo. Este test existe para que la espera no vuelva por inercia: si alguien la
# reintroduce, que sea con un numero nuevo y no con el que ya se midio.

_SUPERFICIE_BORRADA = (
    "UMBRAL_ESPERA_SEGUNDOS", "PRORROGA_SEGUNDOS", "VENTANA_ESPERA_HORAS", "TIPO_VISTA",
    "clave_espera", "candidatos_espera", "filtrar_espera", "mensaje_espera", "confirmados",
    "_ESPERA_SQL",
)


def test_la_alerta_de_ESPERA_ya_no_existe():
    presentes = [n for n in _SUPERFICIE_BORRADA if hasattr(alertas, n)]
    assert presentes == [], (
        f"la espera se borro el 2026-08-31 y volvio: {presentes}. "
        "132 deberian dispararse en 30 dias y llegaban 3.")


def test_el_barrido_NO_devuelve_la_clave_de_la_alerta_EN_VIVO():
    """La forma del retorno la lee `worker.py` para loguear. La clave `espera` --la alerta
    en vivo, borrada-- no puede quedar colgada del otro lado; `espera_larga` es otra cosa
    y esa SI va."""
    cur = _FakeCursor()
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ))
    assert set(r) == {"espera_larga", "resumen", "fallos", "sembrados"}
    assert "espera" not in r


def test_el_barrido_NUNCA_escribe_una_fila_de_espera(monkeypatch):
    """El ledger es compartido con el resumen. Que no quede ni la anotacion `espera_vista`,
    que era gratis de escribir y ahora no la lee nadie."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cur = _FakeCursor()
    alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                   ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ))
    # Se miran los PARAMETROS, no el SQL: el texto de la consulta menciona
    # `espera_de_horario` en un comentario y eso no escribe ninguna fila.
    tipos = [p[1] for p in cur.params if isinstance(p, tuple) and len(p) == 3]
    assert all(t == "resumen" for t in tipos), tipos


# --- LA ESPERA LARGA, MARCADA EN EL RESUMEN (2026-08-31) --------------------
#
# LO QUE PIDIO EL NEGOCIO, textual: "para eso usa la misma plantilla de alerta solo que le
# aumentas cuanto tuvo que esperar para que le respondieran y quien lo hizo, tambien toma
# en cuenta el horario del operador para esto".
#
# ES LA VERSION QUE SI SE PUEDE. La alerta de espera EN VIVO se borro porque el pipeline
# llega tarde por aritmetica (132 deberian / 3 llegan). Esta es retrospectiva: la charla ya
# cerro, el numero esta MEDIDO y no inferido, y no le pide a nadie que corra. Por eso no
# tiene la clase de falso positivo que preocupa -- no puede acusar a alguien que si atendio.
#
# Y EL HORARIO NO ES UN DETALLE, ES LA DIFERENCIA ENTRE INFORMAR Y DIFAMAR. `metrics.
# first_response_seconds` es RELOJ DE PARED y no descuenta la noche. CASO REAL de la copia
# (`05123a61`): el cliente escribio 00:06 y la operadora contesto 06:05 -- reloj de pared
# 359 minutos, pero el negocio abre 06:00, asi que la espera de horario son 5,7 minutos.
# Marcar "esperó 6 horas" en el grupo donde lee gerencia es acusar a quien contesto a los
# seis minutos de abrir. Se mide con `horario.espera_efectiva`, la MISMA funcion con la que
# la rubrica califica: una segunda version del contrato es como se rompen estas cosas.

def _resumen(**kw):
    base = {"username": "u", "ranking": 1, "agencia": "A", "motivo_vip": "M",
            "operador": "Andree", "motivo": "deposito", "stars": 4,
            "first_response_seconds": 30, "resolution_seconds": 120}
    return {**base, **kw}


def test_una_espera_LARGA_se_marca_y_dice_QUIEN_respondio():
    """El negocio pidio las dos cosas: cuanto espero y quien lo atendio."""
    t = alertas.mensaje_resumen(_resumen(
        first_response_seconds=720,
        conversation_created_at=datetime(2026, 8, 28, 18, 5, tzinfo=TZ)))
    assert "12 min" in t
    assert "Andree" in t
    assert alertas.MARCA_ESPERA_LARGA in t


def test_una_respuesta_RAPIDA_no_marca_nada():
    """El 97,4% de los resumenes VIP entra por aca: si se marcaran todos, la marca no
    distingue nada."""
    t = alertas.mensaje_resumen(_resumen(
        first_response_seconds=30,
        conversation_created_at=datetime(2026, 8, 28, 18, 5, tzinfo=TZ)))
    assert alertas.MARCA_ESPERA_LARGA not in t


def test_el_RELOJ_DE_PARED_de_la_madrugada_NUNCA_se_publica():
    """EL CASO `05123a61`, real: escribio 00:06 y le contestaron 06:05. Reloj de pared
    **359 minutos**; de horario, **5,7**. Los dos numeros describen el mismo hecho y solo
    uno es cierto: publicar "6 h" acusa de negligencia a quien contesto a los seis minutos
    de abrir el turno.

    QUE SI PASA: 5,7 min supera los 5 de la vara, asi que la marca VA -- pero diciendo la
    cifra de horario, que es la que el operador puede defender."""
    t = alertas.mensaje_resumen(_resumen(
        operador="Anya Alexandra",
        first_response_seconds=359 * 60,
        conversation_created_at=datetime(2026, 8, 12, 0, 6, 33, tzinfo=TZ)))
    # La cifra de reloj de pared, escrita por el MISMO formateador: si aparece, aparecio
    # tal cual. (Compararla contra literales sueltos como "6 h" se rompe con " horario".)
    assert alertas.duracion(359 * 60) not in t, "el reloj de pared es la cifra que difama"
    assert "5 min" in t, "la que se publica es la de horario: 333 s"
    assert alertas.MARCA_ESPERA_LARGA in t


def test_una_espera_que_ARRANCA_antes_de_abrir_no_se_infla_por_encima_del_umbral():
    """EL CASO `27a60e11`, real (Miguel): escribio 05:19 y contesto 06:04. Reloj de pared
    44,8 minutos; de horario, 4 -- el negocio abre 06:00. Por reloj de pared lo marcaria;
    por horario NO llega al umbral, y no llegar es lo correcto."""
    t = alertas.mensaje_resumen(_resumen(
        operador="Miguel",
        first_response_seconds=round(44.8 * 60),
        conversation_created_at=datetime(2026, 8, 29, 5, 19, 11, tzinfo=TZ)))
    assert alertas.MARCA_ESPERA_LARGA not in t
    assert "44 min" not in t and "45 min" not in t


def test_cuando_la_noche_recorta_la_espera_el_mensaje_lo_DICE():
    """Si el tablero muestra el reloj de pared y Telegram muestra otra cosa, el que lee
    piensa que uno de los dos miente. El sufijo explica la diferencia sin un parrafo."""
    t = alertas.mensaje_resumen(_resumen(
        first_response_seconds=359 * 60,
        conversation_created_at=datetime(2026, 8, 12, 0, 6, 33, tzinfo=TZ)))
    assert "de horario" in t
    rapida = alertas.mensaje_resumen(_resumen(
        first_response_seconds=30,
        conversation_created_at=datetime(2026, 8, 28, 18, 5, tzinfo=TZ)))
    assert "de horario" not in rapida, "puesto siempre es ruido en el 97% de los mensajes"


def test_sin_la_hora_en_que_escribio_NO_se_marca():
    """Sin `conversation_created_at` no se puede descontar la noche, y marcar a ciegas es
    justo el falso positivo que este diseño viene a evitar. Ante la duda, no se acusa."""
    t = alertas.mensaje_resumen(_resumen(first_response_seconds=3600))
    assert alertas.MARCA_ESPERA_LARGA not in t


def test_el_umbral_es_el_de_los_5_minutos_del_negocio():
    assert alertas.UMBRAL_ESPERA_LARGA_SEGUNDOS == 300


def test_la_consulta_TRAE_la_hora_en_que_escribio_el_cliente():
    """Sin esta columna la marca no puede descontar la noche y queda muda. Se pide el
    ALIAS y no el nombre crudo: `conversation_created_at` ya aparecia en el WHERE de la
    ventana de la charla, asi que buscarlo a secas pasa sin que la columna se SELECCIONE."""
    assert "AS conversation_created_at" in alertas._RESUMEN_SQL


# --- LA ALERTA RAPIDA DE ESPERA LARGA (2026-08-31) --------------------------
#
# NO ES LA QUE SE BORRO. Aquella preguntaba "¿esta esperando AHORA?" y no podia llegar a
# tiempo. Esta pregunta "¿espero de mas?" DESPUES de que el operador ya respondio: el hecho
# esta cerrado y el numero medido.
#
# POR QUE NO ESPERA AL SCORE, que es lo que la hace util. Salio del negocio: "solo
# necesitamos el inicio y la primera respuesta en si, no es necesario mandar la
# calificacion". Los dos datos estan en `messages`, asi que NO depende de que la sesion sea
# elegible ni de que el worker llegue a calificarla. MEDIDO sobre 30 dias:
#     por el resumen ..... p50 122 min (sesion de 1) a 61 h (sesion de 20+), y 17,9% NUNCA
#     leyendo messages ... p50 2,9 min desde que el ETL captura la respuesta
#
# EL UMBRAL ES 5 MINUTOS, decision del negocio con el numero a la vista: a 5 min son 88
# alertas en 30 dias (2,9/dia) y a 15 serian 8. Textual: "no porque estos son jugadores vip,
# hay que darle prioridad si esperan mucho hay que alertar, 5 esta bien".
#
# LA VENTANA DE LA CONSULTA ES ANCHA A PROPOSITO, y es la leccion del 17,9%: cuando el
# productor tarda mas que la ventana, la ventana deja de proteger y empieza a BORRAR filas
# en silencio. Lo que evita el duplicado es el ledger, no una ventana angosta.

def _espera(**kw):
    base = {"ticket_id": "tkt-1", "username": "u", "ranking": 1, "agencia": "A",
            "motivo_vip": "M", "operador": "Andree", "cliente": "Juan Pérez",
            "account": "sistemas", "media_type": "chat", "body": "Una recarga por favor",
            "cliente_at": datetime(2026, 8, 28, 14, 0, tzinfo=TZ),
            "respuesta_at": datetime(2026, 8, 28, 14, 12, tzinfo=TZ)}
    return {**base, **kw}


def test_el_umbral_de_la_alerta_rapida_es_el_de_los_5_minutos_del_negocio():
    assert alertas.UMBRAL_ESPERA_LARGA_SEGUNDOS == 300


def test_una_espera_de_12_min_con_un_pedido_real_ALERTA():
    assert alertas.merece_alerta_de_espera(_espera()) is True


def test_una_espera_CORTA_no_alerta():
    assert alertas.merece_alerta_de_espera(_espera(
        respuesta_at=datetime(2026, 8, 28, 14, 3, tzinfo=TZ))) is False


def test_un_GRACIAS_no_alerta_por_mas_que_tarden(): 
    """CAPA 1, `client_sin_motivo`: el cliente no pidio nada, asi que no estaba esperando.
    De los 121 candidatos de 30 dias, esta compuerta mata 32."""
    assert alertas.merece_alerta_de_espera(_espera(body="Ok gracias")) is False


def test_la_NOCHE_no_cuenta_como_espera():
    """`espera_efectiva`, la misma funcion de la rubrica. Escribio 23:58 y le contestaron
    06:02: el reloj de pared dice 6 h 4 min y de horario son CUATRO minutos --dos antes de
    cerrar y dos despues de abrir--, asi que no llega al umbral. Sin esto, cada mañana al
    abrir el turno seria una tormenta de alertas por gente que durmio."""
    assert alertas.merece_alerta_de_espera(_espera(
        cliente_at=datetime(2026, 8, 28, 23, 58, tzinfo=TZ),
        respuesta_at=datetime(2026, 8, 29, 6, 2, tzinfo=TZ))) is False


def test_pero_la_espera_que_CRUZA_la_noche_y_igual_es_larga_SI_alerta():
    """Control negativo del test de arriba: si no fuera por el umbral y la resta, el de
    arriba pasaria por cualquier motivo. Escribio 23:50 y le contestaron 06:02: doce
    minutos de horario, y esos doce SI valen."""
    assert alertas.merece_alerta_de_espera(_espera(
        cliente_at=datetime(2026, 8, 28, 23, 50, tzinfo=TZ),
        respuesta_at=datetime(2026, 8, 29, 6, 2, tzinfo=TZ))) is True


def test_una_espera_de_mas_de_6_HORAS_no_es_una_espera_sino_OTRA_conversacion():
    """CASO REAL (`carlosvilla0105`): "Muchas gracias" y la "respuesta" 1.105 minutos
    despues. Eso no es que lo hayan hecho esperar 18 horas: es que volvio a escribir al dia
    siguiente. El tope es `SILENCIO_MAX`, el mismo con el que se corta la interaccion."""
    assert alertas.merece_alerta_de_espera(_espera(
        body="Una recarga por favor",
        cliente_at=datetime(2026, 8, 28, 7, 0, tzinfo=TZ),
        respuesta_at=datetime(2026, 8, 28, 20, 0, tzinfo=TZ))) is False


# EL GUARD DE LA "PLANTILLA DEL OPERADOR" SE BORRO (2026-08-31), Y ERA UN ERROR MIO.
#
# Lo construi leyendo un mensaje TRUNCADO a 90 caracteres: `*Anya Alexandra:* *Para procesar
# tu retiro*, por favor completa los siguientes datos:` con `from_me = false`. Lo lei como
# "el CRM metio el mensaje del negocio del lado del cliente".
#
# EL TEXTO COMPLETO DECIA OTRA COSA:
#     *Anya Alexandra:*
#     *Para procesar tu retiro*, por favor completa los siguientes datos:
#     Monto a retirar:5
#     Nombres:césar Fernando   Apellidos: pivaque toala
#     Cédula:1312387838        Banco:pichincha
#     Número de cuenta:2204772209
# Es el CLIENTE copiando la plantilla --firma incluida-- y llenandola. `from_me = false`
# esta BIEN, `user_id` es NULL, y Michelle le respondio 7 min 36 s despues. Era una alerta
# LEGITIMA que el guard suprimia.
#
# MEDIDO antes de borrarlo, sobre 30 dias: de los 107 mensajes de tickets VIP que el guard
# descartaba, **106 traen la cedula o la cuenta del cliente llenas**. El guard mataba 106
# retiros reales para atrapar 1 dudoso. En la poblacion general son 238 de 400.
#
# Este test existe para que no vuelva: el pedido de retiro mas explicito que hay no se puede
# descartar por empezar con un asterisco.

def test_el_FORMULARIO_DE_RETIRO_lleno_por_el_cliente_ALERTA():
    """Aunque arranque con la firma de la operadora: el cliente copio la plantilla."""
    assert alertas.merece_alerta_de_espera(_espera(
        body=("*Anya Alexandra:*\n*Para procesar tu retiro*, completa los datos:\n"
              "Monto a retirar:5\nNombres:césar Fernando\nCédula:1312387838\n"
              "Número de cuenta:2204772209"))) is True


def test_la_ficha_con_los_CAMPOS_como_prefijo_tambien_alerta():
    """`*Monto:*` y `*Nombre de Agencia:*` tienen la misma forma que una firma y son la
    ficha que el cliente completo. Son 267 mensajes en 30 dias."""
    assert alertas.merece_alerta_de_espera(_espera(
        body="*Monto:* $1000 *Nombre completo: Joseph Delgado *Cedula: 1313458240")) is True
    assert alertas.merece_alerta_de_espera(_espera(
        body="*Nombre de Agencia:* AGCOX *Banco:* Pichincha *Cuenta:* Ahorros")) is True


def test_el_guard_de_la_plantilla_NO_volvio():
    borrados = [n for n in ("_FIRMA_RE", "_FIRMAS_SQL", "_PREFIJO_NOTA_CRM",
                            "firmas_conocidas", "firmas_del_negocio", "_es_del_negocio")
                if hasattr(alertas, n)]
    assert borrados == [], (
        f"volvio el guard de la plantilla: {borrados}. Mataba 106 retiros reales de 107.")


def test_sin_las_DOS_puntas_no_se_alerta():
    """Sin una de las dos horas no hay espera que medir, y medir a medias es inventar."""
    assert alertas.merece_alerta_de_espera(_espera(cliente_at=None)) is False
    assert alertas.merece_alerta_de_espera(_espera(respuesta_at=None)) is False


def test_el_mensaje_dice_CUANTO_espero_y_QUIEN_respondio():
    t = alertas.mensaje_espera_larga(_espera())
    assert t.startswith(alertas.MARCA_ESPERA_LARGA)
    assert "12 min" in t
    assert "Andree" in t
    assert '"Juan Pérez"' in t and "sistemas" in t, "con que abrirlo en el tablero"


def test_el_mensaje_NO_lleva_el_texto_del_chat_tampoco_aca():
    """Misma regla que el resumen: el cuerpo llevaria cedulas y cuentas a un grupo donde
    nadie las tapa."""
    t = alertas.mensaje_espera_larga(_espera(
        body="mi cedula es 1712345678 y la cuenta 2205232155"))
    assert "1712345678" not in t and "2205232155" not in t


def test_la_clave_del_ledger_es_el_EPISODIO_no_el_ticket():
    """Si fuera solo el ticket, el mismo jugador no volveria a alertar nunca."""
    a = datetime(2026, 8, 28, 14, 0, tzinfo=TZ)
    b = datetime(2026, 8, 29, 14, 0, tzinfo=TZ)
    assert alertas.clave_espera_larga("tkt-1", a) != alertas.clave_espera_larga("tkt-1", b)
    assert alertas.clave_espera_larga("tkt-1", a) == alertas.clave_espera_larga("tkt-1", a)


def test_la_consulta_de_espera_larga_TRAE_los_MENSAJES_con_sus_notas():
    """El corte en interacciones necesita las NOTAS (`*resuelto*`, `*reabierto*`): filtrar
    `is_note` en la consulta dejaria a `partir_en_interacciones` sin con que cortar."""
    sql = alertas._ESPERA_LARGA_SQL
    for col in ("AS ticket_id", "m.created_at", "m.from_me", "m.body", "m.media_type",
                "AS is_note", "AS operador", "AS cliente", "AS account"):
        assert col in sql, col
    assert "coalesce(m.is_note, false) = false" not in sql, "las notas NO se filtran"
    assert "ORDER BY t.id, m.created_at" in sql, "agrupar por ticket pide orden estable"
    assert "make_interval(hours => %(lookback_h)s)" in sql
    assert "make_interval(hours => %(ventana_h)s)" in sql
    assert "es_vip" in sql


def test_el_lookback_es_MAS_ANCHO_que_la_ventana_para_que_el_corte_tenga_contexto():
    """Una interaccion que arranco hace 26 h y sigue viva se partiria mal si el transcript
    empieza justo en el borde de la ventana."""
    class _Cur(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            self.description = [type("D", (), {"name": c})
                                for c in ("ticket_id", "created_at", "from_me", "body",
                                          "media_type", "is_note")]
    cur = _Cur()
    alertas.candidatos_espera_larga(cur, "sistemas",
                                    ahora=datetime(2026, 8, 26, 14, 0, tzinfo=TZ))
    p = cur.params[0]
    assert p["lookback_h"] > p["ventana_h"]


def _mensajes_de_una_espera():
    """Lo que devuelve `_ESPERA_LARGA_SQL`: MENSAJES del ticket, no pares ya resueltos.
    El corte en interacciones lo hace Python."""
    cols = ("ticket_id", "created_at", "from_me", "body", "media_type", "is_note",
            "username", "ranking", "agencia", "motivo_vip", "operador", "cliente", "account")
    fijos = ("u", 1, "A", "M", "Andree", "Juan", "sistemas")
    return cols, [
        ("tkt-1", datetime(2026, 8, 26, 14, 0, tzinfo=TZ), False,
         "Una recarga por favor", "chat", False) + fijos,
        ("tkt-1", datetime(2026, 8, 26, 14, 12, tzinfo=TZ), True,
         "ya se la acredito", "chat", False) + fijos,
    ]


def test_el_barrido_manda_la_espera_larga_y_la_MARCA(monkeypatch):
    """La marca va ANTES del envio: un fallo de red despues de mandar dejaria la alerta sin
    rastro y el proximo barrido --sesenta segundos mas tarde-- la repetiria."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cols, filas = _mensajes_de_una_espera()
    cur = _FakeCursor(rows=filas, cols=cols, para="espera_larga")
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 15, 0, tzinfo=TZ), ledger_vacio_=False)
    assert r["espera_larga"] == 1
    escrito = [p for p in cur.params if isinstance(p, tuple) and len(p) == 3]
    assert ("sistemas", "espera_larga", "tkt-1:2026-08-26T14:00:00-05:00") in escrito


def test_el_barrido_NO_repite_una_espera_ya_avisada(monkeypatch):
    """`marcar_enviada` devuelve False cuando la clave ya estaba: ese es el guardian, no el
    SQL. Sin el, el mismo aviso saldria una vez por minuto."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cols, filas = _mensajes_de_una_espera()

    class _YaEstaba(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "INSERT INTO alertas_enviadas" in " ".join(str(sql).split()):
                self.rowcount = 0      # ON CONFLICT DO NOTHING: no inserto

    cur = _YaEstaba(rows=filas, cols=cols, para="espera_larga")
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 15, 0, tzinfo=TZ), ledger_vacio_=False)
    assert r["espera_larga"] == 0


def test_el_PRIMER_arranque_tampoco_replica_esperas(monkeypatch):
    """Con la ventana de 24 h, encender el bot en una base sin ledger mandaria el backlog
    del dia entero de un saque. La siembra tapa eso: marca y no manda."""
    llamadas = []
    monkeypatch.setattr(alertas.httpx, "post", lambda *a, **k: llamadas.append(a))
    cols, filas = _mensajes_de_una_espera()
    cur = _FakeCursor(rows=filas, cols=cols, para="espera_larga")
    r = alertas.barrer(_ConnFalsa(cur), "sistemas", alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 15, 0, tzinfo=TZ), ledger_vacio_=True)
    assert r["espera_larga"] == 0 and r["sembrados"] >= 1
    assert llamadas == []


def test_el_tope_es_SILENCIO_MAX_y_no_una_copia():
    """Si alguien mueve el silencio con el que se corta la interaccion, el tope de la
    espera tiene que moverse solo. Dos numeros iguales escritos en dos lados divergen."""
    from src.interacciones import SILENCIO_MAX
    assert alertas.TOPE_ESPERA_SEGUNDOS == SILENCIO_MAX.total_seconds()


# --- EL CANDIDATO ES LA INTERACCION, NO EL TURNO (2026-08-31) ---------------
#
# CORRECCION DEL NEGOCIO, textual: "si viene y se guarda el primer mensaje y no llega la
# respuesta se debe enviar la alerta porque es el principio de la respuesta, pero si se
# guarda un mensaje de una conversacion iniciada no porque no tiene nada que ver, ya es el
# inicio y la primera respuesta de una interaccion lo que importa".
#
# LA PRIMERA VERSION MEDIA CUALQUIER TURNO. Sobre 7 dias reales alertaba por "ID:
# 5336766639", "Me confirma" y "Qué el juego quedó 68 a 68": mensajes del MEDIO de una
# charla donde el operador ya estaba adentro. Eso no es una espera por atencion, es el ida
# y vuelta normal de una conversacion, y castigarlo es castigar a alguien que ya atendio.
#
# LO QUE SE MIDE ES EL MISMO RELOJ QUE CALIFICA LA RUBRICA: inicio de la interaccion ->
# primera respuesta del operador. Se corta con `interacciones.partir_en_interacciones` y se
# leen los tiempos con `interacciones.tiempos_de`, las MISMAS funciones del scoring. Ese
# corte sabe cosas que una consulta no: la nota `*resuelto*` cierra, `*reabierto*` delata
# el cierre que no pego, el silencio de 6 h parte, y el adjunto de los 120 s no arranca una
# interaccion nueva.

def _m(minuto, from_me=False, body="Una recarga por favor", is_note=False, media="chat"):
    return {"created_at": datetime(2026, 8, 28, 14, minuto, tzinfo=TZ), "from_me": from_me,
            "body": body, "is_note": is_note, "media_type": media}


def test_solo_el_ARRANQUE_de_la_interaccion_es_candidato():
    """Una charla: el cliente abre, le contestan a los 12 min, y despues hay ida y vuelta.
    Sale UN candidato --el arranque-- y no uno por cada turno del cliente."""
    msgs = [_m(0), _m(12, from_me=True, body="ya le acredito"),
            _m(20, body="ID: 5336766639"), _m(40, from_me=True, body="listo")]
    cands = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})
    assert len(cands) == 1
    assert cands[0]["cliente_at"] == _m(0)["created_at"]
    assert cands[0]["respuesta_at"] == _m(12, from_me=True)["created_at"]


def test_el_turno_del_MEDIO_no_genera_alerta_aunque_tarden():
    """El caso que el negocio marco: los 20 minutos entre 'ID: ...' y la respuesta no son
    una espera por atencion. El operador ya estaba adentro."""
    msgs = [_m(0), _m(1, from_me=True, body="ya la reviso"),
            _m(20, body="ID: 5336766639"), _m(50, from_me=True, body="listo")]
    cands = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})
    assert len(cands) == 1, "una sola interaccion, un solo candidato"
    assert cands[0]["respuesta_at"] == _m(1, from_me=True)["created_at"], \
        "el reloj es el de la PRIMERA respuesta, no el del turno lento"
    assert alertas.merece_alerta_de_espera(cands[0]) is False


def test_una_interaccion_NUEVA_tras_el_cierre_vuelve_a_contar():
    """Despues del `*resuelto*` la atencion siguiente tiene su propio reloj: si el cliente
    vuelve y lo hacen esperar, eso SI es una espera por atencion."""
    msgs = [_m(0), _m(1, from_me=True, body="listo"),
            {"created_at": datetime(2026, 8, 28, 14, 2, tzinfo=TZ), "from_me": True,
             "body": "Andree *resuelto* la conversación", "is_note": True},
            _m(30, body="Otra recarga por favor"),
            _m(45, from_me=True, body="va")]
    cands = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})
    assert len(cands) == 2
    assert alertas.merece_alerta_de_espera(cands[0]) is False, "1 min"
    assert alertas.merece_alerta_de_espera(cands[1]) is True, "15 min tras el cierre"


def test_la_interaccion_SIN_respuesta_todavia_no_alerta_pero_se_devuelve():
    """`respuesta_at` en None es "nadie contesto todavia". `merece_alerta_de_espera` no la
    manda --no se puede medir una espera que no termino, y esa es la alerta EN VIVO que se
    borro por imposible-- pero el candidato se devuelve para que el dia que el negocio la
    quiera no haya que rehacer el corte."""
    cands = alertas.esperas_de_apertura([_m(0), _m(5, body="ahi?")], {"ticket_id": "tkt-1"})
    assert len(cands) == 1 and cands[0]["respuesta_at"] is None
    assert alertas.merece_alerta_de_espera(cands[0]) is False


def test_la_interaccion_que_ABRE_el_negocio_no_es_una_espera():
    """Si el primer mensaje real es del operador --una campaña, un seguimiento-- no hubo
    nadie esperando: el reloj de la primera respuesta no aplica."""
    msgs = [_m(0, from_me=True, body="Hola, le escribo de Sorti"), _m(30, body="dale")]
    assert alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"}) == []


def test_el_candidato_arrastra_los_datos_del_ticket_para_el_mensaje():
    fila = {"ticket_id": "tkt-9", "username": "u", "ranking": 3, "agencia": "A",
            "motivo_vip": "M", "operador": "Andree", "cliente": "Juan", "account": "datos"}
    c = alertas.esperas_de_apertura([_m(0), _m(12, from_me=True, body="va")], fila)[0]
    for k, v in fila.items():
        assert c[k] == v


# --- QUIEN RESPONDIO ES EL QUE RESPONDIO (2026-08-31) -----------------------
#
# EL BUG, encontrado leyendo los 15 transcripts reales de la evaluacion: la alerta nombraba
# al operador ASIGNADO AL TICKET (`tickets.user_id` -> `usr.name`), no al que contesto esa
# interaccion. **En 11 de 15 era otra persona.** Casos reales: la alerta decia "Andree" y
# habia contestado Alejandra; decia "Alex" y fue Salome Ramirez; decia "Miguel" y fue Mario.
#
# ES EL PEOR FALSO POSITIVO POSIBLE en este canal: no se equivoca en el numero, se equivoca
# en la PERSONA, y el mensaje va a un grupo donde lee gerencia. El negocio lo dijo asi:
# "cuida los falsos positivos porque eso es grave y me puede meter en problemas".
#
# EL DATO CORRECTO ES `messages.user_id` DE LA PRIMERA RESPUESTA. Poblado en el 99,8% de los
# mensajes del negocio (1.725 de 1.728 en 7 dias de VIP), y es mas preciso que las dos
# fuentes que ya divergian el 2026-08-27 (`users.name` del ticket y `cs.user_name` del
# scoring): no describe el envase ni el momento del scoreo, describe QUIEN ESCRIBIO ESE
# MENSAJE. Si falta, se cae al del ticket antes que quedarse mudo.

def test_nombra_a_QUIEN_CONTESTO_y_no_al_asignado_al_ticket():
    """El ticket esta asignado a Andree; la primera respuesta la escribio Alejandra. El caso
    real `0989947928`, el retiro de $300 que espero 1 h 14 min."""
    msgs = [
        {"created_at": datetime(2026, 8, 25, 12, 11, tzinfo=TZ), "from_me": False,
         "body": "Hola un retiro por favor", "is_note": False, "media_type": "chat",
         "autor": None},
        {"created_at": datetime(2026, 8, 25, 13, 26, tzinfo=TZ), "from_me": True,
         "body": "Buenas tardes", "is_note": False, "media_type": "chat",
         "autor": "Alejandra"},
    ]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1", "operador": "Andree"})[0]
    assert c["operador"] == "Alejandra"
    assert "Alejandra" in alertas.mensaje_espera_larga(c)
    assert "Andree" not in alertas.mensaje_espera_larga(c)


def test_sin_autor_en_el_mensaje_cae_al_operador_del_ticket():
    """`messages.user_id` viene NULL en el 0,2%. Quedarse mudo es peor que decir el del
    ticket: la alerta sigue sirviendo y el nombre es el mejor que tenemos."""
    msgs = [
        {"created_at": datetime(2026, 8, 25, 12, 11, tzinfo=TZ), "from_me": False,
         "body": "Hola un retiro por favor", "is_note": False, "media_type": "chat"},
        {"created_at": datetime(2026, 8, 25, 13, 26, tzinfo=TZ), "from_me": True,
         "body": "Buenas tardes", "is_note": False, "media_type": "chat", "autor": None},
    ]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1", "operador": "Andree"})[0]
    assert c["operador"] == "Andree"


def test_la_NOTA_no_cuenta_como_primera_respuesta_para_nombrar():
    """`*Asignado automáticamente* a X` la escribe el CRM, no X. Nombrar a esa persona es
    cobrarle una respuesta que no dio."""
    msgs = [
        {"created_at": datetime(2026, 8, 25, 12, 11, tzinfo=TZ), "from_me": False,
         "body": "Una recarga", "is_note": False, "media_type": "chat"},
        {"created_at": datetime(2026, 8, 25, 12, 11, 1, tzinfo=TZ), "from_me": True,
         "body": "*Asignado automáticamente* a Gabriela", "is_note": True,
         "media_type": None, "autor": "Gabriela"},
        {"created_at": datetime(2026, 8, 25, 12, 20, tzinfo=TZ), "from_me": True,
         "body": "Buenas", "is_note": False, "media_type": "chat", "autor": "Mario"},
    ]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1", "operador": "Andree"})[0]
    assert c["operador"] == "Mario"


def test_la_consulta_TRAE_el_autor_de_cada_mensaje():
    assert "AS autor" in alertas._ESPERA_LARGA_SQL
    assert "autor.id = m.user_id" in alertas._ESPERA_LARGA_SQL


# --- LA CORTESIA SE JUZGA SOBRE TODO LO QUE DIJO EL CLIENTE (2026-08-31) ----
#
# FALSO NEGATIVO ENCONTRADO EN LA EVALUACION, y era el peor de los 7 dias. El VIP #2 del
# ranking escribio:
#     12:02:39  "Buenas"
#     12:02:53  "Realice una apuesta de tenis"
#     12:02:57  "Pero el tenista se retiró"
#     12:03:05  "Entonces ahi me devuelven lo apostado"
#     12:03:08  "Pero en qué tiempo"
#     12:05:29  (imagen de la apuesta)
#     12:07:53  "Holaaaa"
#     12:10:36  "Buenasssss"
#     13:21:27  Gabriela responde        -> 1 h 18 min
# La alerta NO salia, porque la compuerta de cortesia miraba SOLO el primer mensaje y el
# primero era "Buenas". Un reclamo con comprobante y dos insistencias quedaba mudo.
#
# `signals.client_sin_motivo` RECIBE UNA LISTA justamente para esto: decide sobre todo lo
# que el cliente planteo, no sobre su saludo. Se le pasan todos los mensajes hasta la
# primera respuesta -- que es el tramo en el que el cliente estuvo esperando.
#
# Sobre 7 dias esto recupera 3 de los 4 falsos negativos por cortesia. El que queda afuera
# es un "?" solo, y afuera esta bien: el propio operador contesto "Me indica de mejor
# manera su solicitud".

def _cli(minuto, body, media="chat"):
    return {"created_at": datetime(2026, 8, 28, 14, minuto, tzinfo=TZ), "from_me": False,
            "body": body, "is_note": False, "media_type": media}


def test_un_SALUDO_seguido_de_un_reclamo_SI_alerta():
    """El caso del VIP #2. Lo que decide es el reclamo, no con que abrio."""
    msgs = [_cli(0, "Buenas"), _cli(1, "Realice una apuesta y el tenista se retiró"),
            _cli(2, "Entonces ahi me devuelven lo apostado"),
            {"created_at": datetime(2026, 8, 28, 15, 18, tzinfo=TZ), "from_me": True,
             "body": "Agradecemos tu espera", "is_note": False, "media_type": "chat",
             "autor": "Gabriela"}]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})[0]
    assert alertas.merece_alerta_de_espera(c) is True


def test_un_saludo_SOLO_sigue_sin_alertar():
    """Control negativo del test de arriba: si recuperara todo, no estaria filtrando nada.
    Un "Buenas" y nada mas no es alguien esperando por algo."""
    msgs = [_cli(0, "Buenas"),
            {"created_at": datetime(2026, 8, 28, 14, 30, tzinfo=TZ), "from_me": True,
             "body": "Buenas tardes", "is_note": False, "media_type": "chat",
             "autor": "Gabriela"}]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})[0]
    assert alertas.merece_alerta_de_espera(c) is False


def test_lo_que_el_cliente_dijo_DESPUES_de_que_le_contestaron_no_cuenta():
    """El tramo que importa es hasta la PRIMERA respuesta: lo de despues ya no es espera, y
    dejarlo entrar convertiria cualquier charla larga en una alerta."""
    msgs = [_cli(0, "Buenas"),
            {"created_at": datetime(2026, 8, 28, 14, 30, tzinfo=TZ), "from_me": True,
             "body": "Buenas tardes", "is_note": False, "media_type": "chat",
             "autor": "Gabriela"},
            _cli(40, "necesito una recarga urgente")]
    c = alertas.esperas_de_apertura(msgs, {"ticket_id": "tkt-1"})[0]
    assert alertas.merece_alerta_de_espera(c) is False



# --- EL LEDGER SE SIEMBRA POR TIPO, NO POR CUENTA (2026-08-31) --------------
#
# PEDIDO DEL NEGOCIO: "las alertas deben ser con lo que vayan llegando, no deben
# acumularse, las que se envian son por el realtime".
#
# `ledger_vacio` miraba la CUENTA. Con 207 filas de `resumen` ya escritas, un tipo NUEVO
# --la espera larga-- encuentra el ledger "no vacio", se saltea la siembra y su primer
# barrido manda el backlog de 24 h de una. Medido sobre 7 dias: 2,7 alertas por dia de
# promedio y hasta 5 en la peor ventana de 24 h. No es una tormenta, pero es historia, y el
# negocio quiere lo que llega.
#
# Mirandolo por (cuenta, TIPO), cada alerta nueva arranca en AHORA: siembra y no manda.

def test_el_ledger_vacio_se_pregunta_por_tipo():
    cur = _FakeCursor(rows=[(207,)], cols=("count",))
    alertas.ledger_vacio(cur, "sistemas", "espera_larga")
    assert cur.params[-1] == ("sistemas", "espera_larga")
    assert "tipo" in cur.sql[-1]


def test_un_tipo_NUEVO_siembra_aunque_la_cuenta_ya_tenga_ledger(monkeypatch):
    """El caso exacto del despliegue: 207 resumenes escritos y CERO esperas largas."""
    monkeypatch.setattr(alertas.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    cols, filas = _mensajes_de_una_espera()

    class _ConCuenta(_FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            texto = " ".join(str(sql).split())
            if "SELECT count(*) FROM alertas_enviadas" in texto:
                # la cuenta tiene resumenes, pero NINGUNA espera larga
                self._rows = [(0,)] if params and params[1] == "espera_larga" else [(207,)]
                self.rowcount = 1

    cur = _ConCuenta(rows=filas, cols=cols, para="espera_larga")
    r = alertas.barrer(_ConCuenta.__mro__ and _ConnFalsa(cur), "sistemas",
                       alertas.Canal("T", "1"),
                       ahora=datetime(2026, 8, 26, 15, 0, tzinfo=TZ))
    assert r["espera_larga"] == 0 and r["sembrados"] >= 1, r
