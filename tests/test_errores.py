"""Los fallos del scoring sobreviven al redeploy: van a la tabla `errors` compartida.

QUE PROBLEMA CIERRA. Hoy cada fallo de una sesion hace `print()` a stdout (`worker.py:603`)
y ahi muere: los logs del contenedor se rotan, asi que un incidente de anteayer no se puede
diagnosticar. El caso que lo prueba esta escrito en `src/llm.py:207`: el 2026-08-25 una misma
sesion fallo **~15 veces en tres horas** y a las 07:25 se sumo una segunda, y eso se
reconstruyo mirando el log EN VIVO. Con la fila persistida es una consulta.
El propio `chat_json` lo dice: *"el log del worker es lo unico que se ve desde afuera"*.

LA TABLA ES DEL ETL Y SE COMPARTE (decision del negocio, 2026-08-28): misma BD, misma tabla,
y una columna `source` nueva ('etl' | 'dashboard') para poder leer un sistema sin el otro.
La columna va con `DEFAULT 'etl'` para que el INSERT que ya tiene `monitor/errors.py` siga
funcionando sin tocar una linea del ETL.

POR QUE ES UN MODULO ESPEJO Y NO UN IMPORT. Los dos repos no comparten dependencia (verificado)
y corren en contenedores separados: compartir el modulo de verdad pediria extraer un paquete,
que toca el build del equipo del ETL. Asi que esto ESPEJA `ETLWhaticket/monitor/errors.py` a
proposito, con la misma API publica y las mismas dos reglas. Es el mismo criterio que ya usa
`plantillas._MAQUINAS` con `metrics._REMITENTES_SIN_PERSONA`: se duplica y se anota, para que
quien toque uno sepa que hay otro.

LAS DOS REGLAS QUE MANDAN, iguales que en el ETL:
  1. `registrar()` NUNCA levanta. Un registrador que explota se lleva puesto el hilo que
     estaba manejando el error original.
  2. Conexion PROPIA y corta. Si el fallo VIENE de la BD, la conexion del llamador quedo en
     transaccion abortada y no acepta el INSERT -- y el worker ya hace `conn.rollback()` en
     su handler justo por eso.
"""
import json

import pytest

from src import errores


class _Cursor:
    def __init__(self, registro, falla=False):
        self._registro = registro
        self._falla = falla

    def execute(self, sql, params=None):
        if self._falla:
            raise RuntimeError("la BD dijo no")
        self._registro.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, registro, falla=False):
        self._registro = registro
        self._falla = falla
        self.commits = 0

    def cursor(self):
        return _Cursor(self._registro, self._falla)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _limpio():
    errores.reset()
    yield
    errores.reset()


def _log(registro, *, falla=False, conectar_falla=False, reloj=None, max_por_ventana=None):
    def connect():
        if conectar_falla:
            raise RuntimeError("no hay red")
        return _Conn(registro, falla)

    kwargs = {"connect": connect}
    if reloj is not None:
        kwargs["reloj"] = reloj
    if max_por_ventana is not None:
        kwargs["max_por_ventana"] = max_por_ventana
    return errores.ErrorLog("dsn-falso", "sistemas", **kwargs)


def _params(registro):
    return registro[-1][1]


# --- lo que persiste ------------------------------------------------------------------

def test_la_fila_se_etiqueta_como_dashboard():
    """LA RAZON DE LA COLUMNA: sin esto, `component` mezcla poller con scoring y no se
    puede leer un sistema sin el otro."""
    registro = []
    assert _log(registro).registrar("scoring", ValueError("boom")) is True
    assert _params(registro)[0] == errores.SOURCE == "dashboard"


def test_persiste_los_campos_de_la_excepcion_y_el_contexto():
    registro = []
    _log(registro).registrar("scoring", ValueError("boom"),
                             context={"session_id": "abc", "interaccion_id": "def"})
    source, account, component, kind, message, context, tb = _params(registro)
    assert (source, account, component, kind) == ("dashboard", "sistemas", "scoring",
                                                 "ValueError")
    assert message == "boom"
    assert json.loads(context) == {"session_id": "abc", "interaccion_id": "def"}
    assert "ValueError" in tb, "no guardo el traceback"


def test_sirve_sin_excepcion_para_dejar_una_nota():
    registro = []
    _log(registro).registrar("alertas", message="el ledger arranco vacio")
    source, _, component, kind, message, _, tb = _params(registro)
    assert (component, kind, message, tb) == ("alertas", None,
                                              "el ledger arranco vacio", None)


def test_recorta_el_texto_gigante_CONTANDO_el_sufijo():
    """El tope tiene que ser un tope de verdad: un traceback en bucle puede llegar a
    megabytes y la tabla de errores no puede volverse el problema."""
    registro = []
    _log(registro).registrar("scoring", ValueError("x" * 50_000))
    message = _params(registro)[4]
    assert len(message) == errores.MAX_TEXTO
    assert message.endswith("... [recortado]")


# --- la regla 1: nunca levanta --------------------------------------------------------

def test_si_la_bd_falla_devuelve_False_pero_NO_levanta():
    """Un registrador que explota se lleva puesto el hilo que manejaba el error original."""
    assert _log([], falla=True).registrar("scoring", ValueError("boom")) is False


def test_si_conectar_falla_NO_levanta():
    assert _log([], conectar_falla=True).registrar("scoring", ValueError("boom")) is False


def test_sin_configurar_el_helper_es_un_no_op():
    """El worker corre en tests y en local sin BD: no puede exigir configuracion."""
    assert errores.registrar("scoring", ValueError("boom")) is False


def test_configurar_habilita_el_helper_global():
    registro = []
    errores.configurar("dsn-falso", "datos", connect=lambda: _Conn(registro))
    assert errores.registrar("scoring", ValueError("boom")) is True
    assert _params(registro)[1] == "datos"


# --- el limitador ---------------------------------------------------------------------

def test_no_inunda_la_tabla_cuando_falla_un_lote_entero():
    """EL CASO QUE LO JUSTIFICA: el worker registra UNA fila por sesion fallada, dentro del
    bucle del lote. Si una tanda entera falla son miles de filas en segundos."""
    registro = []
    reloj = iter([100.0] * 40)
    log = _log(registro, reloj=lambda: next(reloj), max_por_ventana=3)
    resultados = [log.registrar("scoring", ValueError("boom")) for _ in range(10)]
    assert resultados[:3] == [True, True, True]
    assert resultados[3:] == [False] * 7
    assert len(registro) == 3, "escribio mas filas que el tope de la ventana"


def test_al_cerrar_la_ventana_deja_constancia_de_CUANTAS_suprimio():
    """No se puede perder la magnitud: importa muchisimo si fueron 3 o 40.000."""
    tiempos = iter([100.0, 100.0, 100.0, 100.0, 500.0])
    registro = []
    log = _log(registro, reloj=lambda: next(tiempos), max_por_ventana=2)
    for _ in range(4):          # 2 pasan, 2 se suprimen
        log.registrar("scoring", ValueError("boom"))
    log.registrar("scoring", ValueError("boom"))   # ventana nueva: emite el resumen
    resumen = [p for _, p in registro if p[4] and "suprimieron" in p[4]]
    assert resumen, "no dejo constancia de las suprimidas"
    assert "2" in resumen[0][4], f"no dice cuantas: {resumen[0][4]!r}"
    assert json.loads(resumen[0][5])["suprimidos"] == 2


def test_cada_componente_tiene_su_propio_cupo():
    """Que el scoring inunde no puede tapar un fallo de las alertas."""
    registro = []
    reloj = iter([100.0] * 40)
    log = _log(registro, reloj=lambda: next(reloj), max_por_ventana=1)
    assert log.registrar("scoring", ValueError("a")) is True
    assert log.registrar("scoring", ValueError("b")) is False
    assert log.registrar("alertas", ValueError("c")) is True


def test_cada_clase_de_error_tiene_su_propio_cupo():
    """Un ReadTimeout repetido no puede esconder el primer EmptyCompletionError."""
    registro = []
    reloj = iter([100.0] * 40)
    log = _log(registro, reloj=lambda: next(reloj), max_por_ventana=1)
    assert log.registrar("scoring", TimeoutError("a")) is True
    assert log.registrar("scoring", TimeoutError("b")) is False
    assert log.registrar("scoring", ValueError("c")) is True


# --- EL ORDEN DE DEPLOY, QUE NO SE PUEDE DEDUCIR DEL CODIGO ---------------------------
#
# La columna `source` la crea el `db/schema.sql` del ETL, que corre cuando el MONITOR
# arranca. Si el dashboard sube primero, cada INSERT revienta con UndefinedColumn -- y por la
# regla 1 revienta EN SILENCIO: devuelve False, loguea un warning y la tabla queda vacia sin
# que nadie se entere. Es el mismo modo de falla que ya costo caro con `operator_status`
# ("el PUT devolvia 200 y mentia").
#
# Por eso hay una linea de arranque que lo dice. Mismo patron que
# `operator_status: 35 filas sembradas · apagados por cuenta: {...}`: UNA linea del log que
# distingue "el deploy entro" de "el deploy no entro".

def test_estado_avisa_cuando_la_tabla_esta_lista():
    registro = []
    log = _log(registro)
    assert log.estado() == "tabla `errors` lista (source=dashboard)"


def test_estado_avisa_CLARO_cuando_falta_la_columna_source():
    """El caso del deploy fuera de orden: el mensaje tiene que nombrar la causa y el
    arreglo, no un stacktrace de psycopg."""
    class _CursorSinColumna(_Cursor):
        def execute(self, sql, params=None):
            raise RuntimeError('column "source" of relation "errors" does not exist')

    class _ConnSinColumna(_Conn):
        def cursor(self):
            return _CursorSinColumna(self._registro)

    log = errores.ErrorLog("dsn", "sistemas", connect=lambda: _ConnSinColumna([]))
    estado = log.estado()
    assert "source" in estado
    assert "ETL" in estado, f"no dice de quien depende el arreglo: {estado!r}"


def test_estado_avisa_cuando_no_hay_BD():
    log = _log([], conectar_falla=True)
    assert "no se pudo" in log.estado().lower()


def test_estado_NUNCA_levanta():
    """Es una linea de log en el arranque: no puede impedir que el worker levante."""
    class _ConnRaro:
        def __enter__(self):
            raise RuntimeError("cualquier cosa")

        def __exit__(self, *a):
            return False

    log = errores.ErrorLog("dsn", "s", connect=lambda: _ConnRaro())
    assert isinstance(log.estado(), str)


# --- LAS SEIS REGLAS DEL EQUIPO DEL ETL (2026-08-28) ----------------------------------
#
# Son los dueños de la tabla y las escribieron al aprobar el uso compartido. Cinco ya
# estaban cumplidas; la 6 encontró un hueco real: `configurar()` se llamaba SIN cuenta, así
# que toda fila del dashboard salía con `account = NULL` y eso les rompe su índice
# `idx_errors_cuenta (account, component, occurred_at DESC)`. Y nuestros errores SÍ son por
# cuenta: el loop del worker itera `cfg.scoring_accounts`.

def test_regla_1_no_se_mandan_las_dos_columnas_de_fecha():
    """Las pone Postgres por DEFAULT. Setearlas desde el proceso trae de vuelta el problema
    del reloj del contenedor y, peor, pueden quedar inconsistentes entre sí."""
    for columna in ("occurred_at", "occurred_at_ec"):
        assert columna not in errores._INSERT, (
            f"el INSERT nombra {columna}: la hora tiene que ponerla la BD"
        )


def test_regla_2_el_source_es_lo_primero_y_siempre_dashboard():
    """El default de la columna es 'etl': si no se manda, nuestros errores figuran como
    suyos."""
    registro = []
    _log(registro).registrar("scoring", ValueError("boom"))
    assert errores._INSERT.index("source") < errores._INSERT.index("account")
    assert _params(registro)[0] == "dashboard"


def test_regla_3_el_tope_por_campo_es_el_mismo_que_el_del_etl():
    assert errores.MAX_TEXTO == 8000


def test_regla_4_el_limitador_es_el_mismo_que_el_del_etl():
    """5 filas por (componente, clase) cada 60 s. Su medición: sin límite, 5000 intentos
    son 5000 filas en segundos."""
    assert (errores.MAX_POR_VENTANA, errores.VENTANA) == (5, 60)


def test_regla_6_la_cuenta_va_EN_LA_COLUMNA_por_llamada():
    """Su índice es (account, component, occurred_at). El loop del worker atiende VARIAS
    cuentas con un solo ErrorLog, así que la cuenta no puede fijarse al configurar."""
    registro = []
    log = errores.ErrorLog("dsn", None, connect=lambda: _Conn(registro))
    log.registrar("scoring", ValueError("boom"), account="sistemas")
    assert _params(registro)[1] == "sistemas"
    log.registrar("scoring", ValueError("otro"), account="datos")
    assert _params(registro)[1] == "datos"


def test_regla_6_sin_cuenta_queda_NULL_y_eso_esta_bien():
    """Nullable a propósito: un fallo de arranque no es de una cuenta puntual."""
    registro = []
    errores.ErrorLog("dsn", None, connect=lambda: _Conn(registro)).registrar(
        "arranque", message="no se pudo leer la config")
    assert _params(registro)[1] is None


def test_regla_6_hay_un_vocabulario_declarado_de_componentes():
    """Texto libre en la BD, pero acordado de nuestro lado para que su índice sirva."""
    assert errores.COMPONENTES == ("scoring", "llm", "alertas", "arranque")


def test_regla_6_un_componente_fuera_del_vocabulario_se_registra_igual_pero_avisa(caplog):
    """No se rechaza la fila: perder un error por un nombre mal escrito sería peor que
    tener una fila con un componente raro. Pero queda el warning para que se corrija."""
    registro = []
    log = errores.ErrorLog("dsn", None, connect=lambda: _Conn(registro))
    assert log.registrar("inventado", ValueError("boom")) is True
    assert _params(registro)[2] == "inventado"
    assert any("inventado" in r.getMessage() for r in caplog.records), (
        "no avisó del componente fuera del vocabulario"
    )
