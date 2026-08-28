"""Los fallos del scoring van a la tabla `errors`, que SOBREVIVE al redeploy.

QUE PROBLEMA CIERRA. Hoy cada fallo de una sesion hace `print()` a stdout
(`worker.py:603`) y ahi muere: los logs del contenedor se rotan, asi que un incidente de
anteayer no se puede diagnosticar. El caso que lo prueba esta escrito en `src/llm.py:207`:
el 2026-08-25 una misma sesion fallo **~15 veces en tres horas** y a las 07:25 se sumo una
segunda, y eso se reconstruyo mirando el log EN VIVO. Con la fila persistida es una consulta.
`chat_json` lo dice textual: *"el log del worker es lo unico que se ve desde afuera"*.

Y los fallos del LLM llegan aca solos: `chat_json` LEVANTA `EmptyCompletionError`, que sube
hasta el handler del lote. No hace falta cablear `src/llm.py`.

LA TABLA ES DEL ETL Y SE COMPARTE (decision del negocio, 2026-08-28). Misma BD, misma tabla,
y una columna `source` ('etl' | 'dashboard') para poder leer un sistema sin el otro. La
columna se agrego con `DEFAULT 'etl'` para que el INSERT que ya tiene `monitor/errors.py`
siga andando sin tocar una linea del ETL. Su esquema vive en
`ETLWhaticket/db/schema.sql` — este modulo NO lo crea ni lo migra: es un ESCRITOR invitado, y
crear la tabla desde dos lados es como se pierde el control de una forma.

POR QUE ES UN MODULO ESPEJO Y NO UN IMPORT. Los dos repos no comparten dependencia
(verificado) y corren en contenedores separados: compartir el modulo de verdad pediria
extraer un paquete, que toca el build del equipo del ETL. Asi que esto ESPEJA
`ETLWhaticket/monitor/errors.py` con la misma API publica y las mismas dos reglas. Es el
mismo criterio que ya usa `plantillas._MAQUINAS` con `metrics._REMITENTES_SIN_PERSONA`: se
duplica y se anota, para que quien toque uno sepa que hay otro.

LAS DOS REGLAS QUE MANDAN:

1. `registrar()` NUNCA levanta. Un registrador de errores que explota se lleva puesto el
   hilo que estaba manejando el error original.

2. Conexion PROPIA y corta por escritura. Si el fallo VIENE de la BD, la conexion del
   llamador quedo en transaccion abortada y no acepta el INSERT — el worker ya hace
   `conn.rollback()` en su handler justo por eso. Y como el worker corre en hilos, compartir
   una conexion psycopg (que no es thread-safe) seria otro bug.

LA HORA LA PONE POSTGRES, no el proceso: las columnas tienen DEFAULT now(). El reloj y la
zona del contenedor son irrelevantes, y las dos cuentas quedan en la misma escala.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback as _traceback

log = logging.getLogger(__name__)

# Que sistema escribe. Es una constante y no un parametro a proposito: este modulo vive en
# el dashboard, y una fila etiquetada 'etl' desde aca seria una mentira imposible de
# rastrear despues.
SOURCE = "dashboard"

# EL VOCABULARIO DE `component`, acordado de nuestro lado (regla 6 del equipo del ETL). En la
# BD la columna es texto libre; el acuerdo es nuestro y sirve para que su indice
# `(account, component, occurred_at DESC)` no se llene de sinonimos.
#   scoring  -> el lote de una sesion o conversacion fallo (el LLM llega aca: `chat_json`
#               levanta EmptyCompletionError y sube al handler del lote)
#   llm      -> un fallo del modelo aislado, fuera del lote
#   alertas  -> el barrido de alertas VIP
#   arranque -> migraciones, sondas y config: NO es de una cuenta puntual
COMPONENTES = ("scoring", "llm", "alertas", "arranque")

# Tope por campo de texto. Un traceback en bucle o un body de respuesta enorme pueden llegar
# a megabytes; la tabla de errores no puede volverse el problema.
MAX_TEXTO = 8000

# Limite por (componente, clase de error). EL CASO QUE LO JUSTIFICA: el worker registra UNA
# fila por sesion fallada, dentro del bucle del lote — si una tanda entera falla son miles de
# filas en segundos. Al cerrar la ventana se emite UNA fila diciendo cuantas se suprimieron,
# asi no se pierde la magnitud: importa muchisimo si fueron 3 o 40.000.
MAX_POR_VENTANA = 5
VENTANA = 60

_INSERT = (
    "INSERT INTO errors (source, account, component, kind, message, context, traceback) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

_SUFIJO = "... [recortado]"


def _recortar(texto):
    """Recorta a MAX_TEXTO CONTANDO el sufijo: el tope tiene que ser un tope de verdad, o el
    campo termina midiendo mas de lo que dice el limite."""
    if texto is None:
        return None
    texto = str(texto)
    if len(texto) <= MAX_TEXTO:
        return texto
    return texto[:MAX_TEXTO - len(_SUFIJO)] + _SUFIJO


def _jsonb(context):
    """El contexto como JSON. Si no es serializable se pierde el contexto, NO la fila: el
    error original importa mas que su metadata."""
    if context is None:
        return None
    try:
        return json.dumps(context, ensure_ascii=False, default=str)
    except Exception:
        return None


def _pg_connect(dsn: str):
    """Import lazy de psycopg, igual que en el ETL: los tests y las herramientas que usan
    este modulo con un `connect` inyectado no necesitan tenerlo."""
    import psycopg

    return psycopg.connect(dsn, connect_timeout=8)


class ErrorLog:
    def __init__(self, dsn: str, account: str | None, connect=None, reloj=None,
                 max_por_ventana: int = MAX_POR_VENTANA, ventana: int = VENTANA):
        self._dsn = dsn
        self._account = account
        self._connect = connect or (lambda: _pg_connect(self._dsn))
        self._reloj = reloj or time.monotonic
        self._max = max_por_ventana
        self._ventana = ventana
        # Estado del limitador por (componente, clase). Lo tocan todos los hilos, de ahi el
        # lock.
        self._cupos: dict[tuple, list] = {}
        self._lock = threading.Lock()

    def _permitido(self, clave) -> tuple[bool, int]:
        """(se puede emitir, cuantas se suprimieron al cerrar la ventana anterior).

        El conteo se emite APARTE para no perder la magnitud del incidente.
        """
        ahora = self._reloj()
        with self._lock:
            estado = self._cupos.get(clave)
            if estado is None or ahora - estado[0] > self._ventana:
                suprimidas = estado[2] if estado else 0
                self._cupos[clave] = [ahora, 1, 0]
                return True, suprimidas
            if estado[1] < self._max:
                estado[1] += 1
                return True, 0
            estado[2] += 1
            return False, 0

    def registrar(self, component: str, exc: BaseException | None = None, *,
                  account: str | None = None, message: str | None = None,
                  context: dict | None = None) -> bool:
        """Deja constancia de un fallo. True si logro persistirlo.

        `account` VA POR LLAMADA (regla 6 del equipo del ETL): su indice es
        `(account, component, occurred_at DESC)`, y el loop del worker atiende VARIAS cuentas
        con un solo ErrorLog — fijarla al configurar dejaba todas las filas en NULL. Se cae al
        default de la instancia si no viene, y queda NULL a proposito cuando el fallo no es de
        una cuenta puntual (un error de arranque, por ejemplo).

        NUNCA levanta: cualquier problema propio se loguea y devuelve False.
        """
        if component not in COMPONENTES:
            # Se registra IGUAL: perder un error por un nombre mal escrito seria peor que
            # tener una fila con un componente raro. El warning es para que se corrija.
            log.warning("componente %r fuera del vocabulario acordado %s; la fila se "
                        "registra igual.", component, COMPONENTES)
        try:
            clave = (component, type(exc).__name__ if exc is not None else None)
            permitido, suprimidas = self._permitido(clave)
            if suprimidas:
                self._escribir(
                    component, None,
                    f"Se suprimieron {suprimidas} errores iguales en los ultimos "
                    f"{self._ventana}s (limite {self._max} por ventana).",
                    {"suprimidos": suprimidas, "ventana_s": self._ventana}, None,
                    account)
            if not permitido:
                return False
        except Exception:
            log.warning("Fallo el limitador de la tabla `errors`.", exc_info=True)
            return False
        kind = type(exc).__name__ if exc is not None else None
        texto = message if message is not None else (str(exc) if exc else None)
        tb = None
        if exc is not None:
            tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        return self._escribir(component, kind, texto, context, tb, account)

    def estado(self) -> str:
        """Una linea para el log de arranque: la tabla acepta escrituras, o por que no.

        EL ORDEN DE DEPLOY NO SE PUEDE DEDUCIR DEL CODIGO, y por eso existe esto. La columna
        `source` la crea el `db/schema.sql` del ETL, que corre cuando el MONITOR arranca. Si
        el dashboard sube primero, cada INSERT revienta con UndefinedColumn — y por la regla 1
        revienta EN SILENCIO: la tabla queda vacia y se ve igual que un dia sin errores. Es el
        mismo modo de falla que ya costo caro con `operator_status`, donde el PUT devolvia 200
        y mentia.

        Mismo patron que la linea de `operator_status`: UNA linea que distingue "el deploy
        entro" de "el deploy no entro". NUNCA levanta: es un log de arranque, no puede
        impedir que el worker levante.

        La prueba es un SELECT y no un INSERT a proposito: verificar escribiendo dejaria una
        fila basura en la bitacora en cada arranque.
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT source FROM errors WHERE false")
            return f"tabla `errors` lista (source={SOURCE})"
        except Exception as e:  # noqa: BLE001 - cualquier fallo se traduce a texto
            detalle = str(e)
            if "source" in detalle:
                return ("tabla `errors` SIN la columna `source`: el dashboard no va a poder "
                        "registrar nada. Falta que el ETL despliegue su schema.sql "
                        "(la crea al arrancar el monitor).")
            return f"no se pudo verificar la tabla `errors`: {type(e).__name__}: {detalle[:200]}"

    def _escribir(self, component, kind, texto, context, tb, account=None) -> bool:
        try:
            params = (
                SOURCE,
                # La de la llamada manda; la de la instancia es el default. Nullable a
                # proposito cuando el fallo no es de una cuenta puntual (regla 6).
                account if account is not None else self._account,
                _recortar(component),
                _recortar(kind),
                _recortar(texto),
                _jsonb(context),
                _recortar(tb),
            )
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_INSERT, params)
                conn.commit()
            return True
        except Exception:
            # A proposito NO se relanza: ver la regla 1 del docstring del modulo.
            log.warning("No se pudo persistir el error en la tabla `errors`.", exc_info=True)
            return False


# --- Helper de modulo -------------------------------------------------------------------
# Mismo patron que `logging` y que el del ETL: se configura UNA vez en el arranque y despues
# cualquier modulo registra sin recibir la instancia por constructor. Sin configurar es un
# no-op, que es lo que permite correr los tests y el local sin BD.

_global: ErrorLog | None = None


def configurar(dsn: str, account: str | None = None, connect=None) -> None:
    global _global
    _global = ErrorLog(dsn, account, connect=connect)


def reset() -> None:
    """Vuelve al estado sin configurar (lo usan los tests)."""
    global _global
    _global = None


def registrar(component: str, exc: BaseException | None = None, *,
              account: str | None = None, message: str | None = None,
              context: dict | None = None) -> bool:
    """Registra en el ErrorLog global. Si nadie llamo a `configurar()`, no-op."""
    if _global is None:
        return False
    return _global.registrar(component, exc, account=account, message=message,
                             context=context)


def estado() -> str:
    """La linea de arranque del ErrorLog global. Ver `ErrorLog.estado`."""
    if _global is None:
        return "bitacora de errores SIN configurar: no se persiste ningun fallo"
    return _global.estado()
