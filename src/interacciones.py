"""Donde EMPIEZA y TERMINA una interaccion, segun el objeto y no segun una heuristica.

EL CONCEPTO ES DEL NEGOCIO (2026-08-11): una interaccion es una vez que el cliente habla
con el operador, y **es el operador el que decide cuando cierra**. Ese cierre esta en los
datos: el CRM escribe una NOTA INTERNA "<Nombre> *resuelto* la conversacion". Hay 409.820
de esas, mas 13.859 de "*reabierto*" -- que confirman el modelo: se cierra, y si el cliente
vuelve se reabre.

POR QUE NADIE LO VEIA. Todas las rubricas filtran `is_note` en su primera linea (`_es_real`
en agilidad, el `if not m.get("is_note")` en deposito/retiro/soporte/info). Es correcto para
no contar una nota como mensaje del operador, pero tiene el efecto lateral de que la
FRONTERA DE LA INTERACCION es invisible para todo el sistema. El dato estaba desde el
principio y se tiraba en el primer filtro.

POR QUE HACE FALTA. En el 96,3% de las conversaciones el objeto ya alcanza: 154.370 de
160.229 tienen UN solo cierre, o sea una interaccion por conversacion. Pero **5.624 (3,51%)
tienen varios**, y ahi viven **1.377.453 mensajes = el 41,7% de todos**: son los clientes
recurrentes, donde el CRM no abre una fila nueva y sigue escribiendo en la misma.
Sin este corte, las rubricas buscan la evidencia en TODA la conversacion y emparejan cosas
de transacciones distintas. Caso `f9b31f4f-6399-4e76-96ce-3a1b726aa7da`: 84 mensajes, 8
dias, 16 cierres y cuatro operadores; un comprobante del 3-ago que nadie contesto se
emparejo con el saludo de otra transaccion del 6-ago y produjo el rationale "confirmo la
acreditacion, pero tardo 39,5 horas en avisarle".

Y ES EL MISMO CORTE QUE TIENE QUE USAR EL FRONT para mostrar las interacciones: si la nota
se calcula por interaccion y la pantalla muestra la conversacion entera, el numero no se
puede verificar mirando el chat -- que es exactamente lo que paso con el caso de arriba.
"""
from __future__ import annotations

import re
from datetime import timedelta

# SEIS HORAS DE SILENCIO CIERRAN LA INTERACCION, aunque el CRM nunca la haya cerrado.
# Decision del negocio (2026-08-24): "que el tiempo sea solo de 6 horas por si alguien
# escribio una respuesta, y de ahi que todo se agarren como interacciones diferentes,
# porque cada interaccion tiene un operador a calificar".
#
# POR QUE HACIA FALTA. El corte por `*resuelto*` es del OBJETO y es el correcto, pero no
# tiene piso: si el operador nunca cierra, todo el transcript es UNA interaccion sin tope de
# ninguna clase. Y `assign_sessions` no lo salva -- su SPAN_CAP de 12h corta entre EPISODIOS
# (filas de `conversations`), asi que en una sesion de un solo episodio no hay frontera donde
# aplicarlo. MEDIDO en la copia del 2026-08-24 sobre 1.431 sesiones cerradas en 4 dias:
# el 10,3% arrastra un stream de MAS DE SIETE DIAS (maximo 6.765 h = 282 dias), y 164 de esas
# 170 tienen un solo episodio. Ahi nacian las filas que declaran "33 interacciones · 10
# operadores" al lado de una nota que juzgo tres minutos de UNA persona.
#
# LAS 6 HORAS SON LA GRACIA DE LA RESPUESTA TARDIA, no un tope arbitrario: dentro de la
# ventana el mensaje todavia pertenece a la interaccion que lo motivo, asi que partir no
# puede fabricar un "nadie le contesto". Pasada la ventana, empieza otra atencion con su
# propio operador a calificar.
SILENCIO_MAX = timedelta(hours=6)

# La nota que el CRM escribe cuando el operador CIERRA. Es la frontera.
_CIERRE_RE = re.compile(r"\*resuelto\*", re.IGNORECASE)
# Y la que escribe cuando el ticket se REABRE. Confirma el modelo del negocio (se cierra, y
# si el cliente vuelve se reabre) pero tambien delata los cierres que NO pegaron.
_REAPERTURA_RE = re.compile(r"\*reabierto\*", re.IGNORECASE)

# CERRAR-Y-ADJUNTAR ES UN SOLO GESTO. El flujo real del operador de retiro es cerrar la
# conversacion y mandar el comprobante inmediatamente despues; la nota y la imagen salen
# con una MEDIANA DE 1,1 SEGUNDOS de diferencia. Con el corte estricto ese comprobante
# caia en la interaccion SIGUIENTE y la rubrica no lo encontraba -> "nunca envio el
# comprobante" para un retiro que si se pago. MEDIDO el 2026-08-12 sobre el rescore v5:
# 42 de 139 retiros en 2 estrellas (32%), el 100% dentro de los 2 minutos del cierre.
# La gracia es SOLO para el operador: si el que vuelve a hablar es el CLIENTE, eso es una
# interaccion nueva por definicion del negocio.
GRACIA_CIERRE_SEG = 120

# LA COLA DE CORTESIA: el "gracias" que el cliente manda DESPUES del cierre.
#
# `GRACIA_CIERRE_SEG` es solo para el operador ("si el que vuelve a hablar es el CLIENTE,
# eso es una interaccion nueva"). Esa regla es correcta para un cliente que VUELVE con algo,
# y falsa para el que agradece: MEDIDO sobre el rescore por interaccion (2026-08-27), el
# corte le ponia **1 estrella por "nadie le respondio"** a quien acababa de acreditar bien,
# 21 segundos antes. Son ~157 casos por mes.
#
# Y NO ES SOLO EL FALSO 1 ESTRELLA. `store.py` declara que `signals.cliente_confirmo_resuelto`
# -- el cliente diciendo "ya pude, gracias" -- es **ground truth del unico que sabe si su
# problema se resolvio**. Cortarlo afuera le ROBA a la atencion anterior su mejor evidencia.
#
# EL UMBRAL SALE DEL DATO, sobre 717 "gracias" tras un cierre en 30 dias, mirando en cuantos
# el que vuelve a cerrar es el MISMO operador (la senal de que es la misma atencion):
#     <= 1 min   270 casos   88,1 por ciento
#     1-5 min    151 casos   88,1 por ciento
#     5-15 min   125 casos   80,8 por ciento
#     > 15 min   171 casos   55,0 por ciento   <- moneda al aire: ahi si volvio de verdad
# A los 5 minutos la continuidad se sostiene; pasados los 15 se cae a la mitad.
GRACIA_CORTESIA_SEG = 300

# Lo que el cliente dice cuando NO esta planteando nada. Se escribe aca y no se importa de
# `signals` por el mismo motivo que `_hubo_negocio` esta duplicado: este modulo es de base y
# lo importan deposito, retiro, agilidad, registro, metrics, worker y queries.
_CORTESIA_RE = re.compile(
    r"^\s*(muchas\s+|mil\s+)?"
    r"(gracias|grax|ok+|oka|listo|dale|bueno|perfecto|excelente|genial|"
    r"bendicion(es)?|saludos|de\s+nada|amen)\b",
    re.IGNORECASE)


def es_cierre(m: dict) -> bool:
    """El mensaje es la nota interna de cierre del operador."""
    return bool(m.get("is_note")) and bool(_CIERRE_RE.search(m.get("body") or ""))


def es_reapertura(m: dict) -> bool:
    """El mensaje es la nota interna de reapertura del CRM."""
    return bool(m.get("is_note")) and bool(_REAPERTURA_RE.search(m.get("body") or ""))


def _cierre_rebotado(ordenados: list[dict], i: int) -> bool:
    """El cierre en `i` NO PEGO: lo reabren enseguida y nadie hablo en el medio.

    El CRM a veces dispara "*resuelto*" y lo "*reabierto*" segundos despues sin que haya
    pasado nada. Tratar eso como frontera parte la interaccion al medio y deja al operador
    con un tramo donde "nadie respondio" -- cuando en realidad la atencion siguio y termino
    bien. MEDIDO el 2026-08-12: 7.406 pares resuelto->reabierto, MEDIANA 58,5 segundos y
    4.884 (66%) dentro de los 2 minutos.
    Si alguien HABLO antes de la reapertura, el cierre fue real y el cliente volvio: esa SI
    es una interaccion nueva. Y la cola larga de reaperturas legitimas (promedio 10 horas)
    queda afuera por la ventana.
    """
    cierre_at = ordenados[i]["created_at"]
    for m in ordenados[i + 1:]:
        if (m["created_at"] - cierre_at).total_seconds() > GRACIA_CIERRE_SEG:
            return False
        if es_reapertura(m):
            return True
        if not m.get("is_note"):
            return False
    return False


def _hubo_negocio(interaccion: list[dict]) -> bool:
    """Alguien del negocio le escribio AL CLIENTE dentro del fragmento.

    LA NOTA DEL CRM NO CUENTA: es `from_me` pero no es un mensaje al cliente. Si contara,
    un fragmento cerrado con `*resuelto*` pareceria respondido y no se pegaria con el que SI
    tiene la respuesta -- o sea, la estrella falsa se fabricaria igual.

    ES EL MISMO PREDICADO que `sin_respuesta.hubo_respuesta_del_negocio` y esta escrito dos
    veces a proposito: este modulo es de base (lo importan deposito, retiro, agilidad,
    registro, metrics, worker y queries) e importar `sin_respuesta` arrastraria `scorer`
    hasta aca. Que no se separen lo fija
    tests/test_continuidad_entre_fragmentos.py::test_la_regla_de_respuesta_del_negocio_es_LA_MISMA_que_la_de_sin_respuesta.
    """
    return any(m.get("from_me") and not m.get("is_note") for m in interaccion)


def _empieza_el_negocio(interaccion: list[dict]) -> bool:
    """El PRIMER mensaje real del fragmento es del negocio -> esta contestando."""
    for m in interaccion:
        if not m.get("is_note"):
            return bool(m.get("from_me"))
    return False


def _pegar_continuaciones(partes: list[list[dict]]) -> list[list[dict]]:
    """Un fragmento sin respuesta del negocio se pega al siguiente si ahi arranca el negocio.

    POR QUE. La frontera del CRM cae a veces ENTRE lo que el cliente dijo y la respuesta del
    operador: cierra despues del comprobante y acredita diez minutos mas tarde, o contesta
    pasadas las 6 horas. Las dos mitades son la MISMA atencion, y calificarlas por separado
    produce dos filas falsas: una que acusa de no responder a quien respondio, y otra sin
    cliente. MEDIDO sobre 1.200 sesiones multi-episodio de la copia: de 3.404 interacciones,
    806 (23,68%) no tenian negocio adentro y **254 tenian la acreditacion en el vecino**.

    ES LA GENERALIZACION DE `GRACIA_CIERRE_SEG`, que ya pegaba hacia adelante el comprobante
    adjuntado en el mismo gesto (dentro de 120s). Aca la evidencia viaja al reves y sin tope
    de tiempo: lo que decide no es el reloj sino QUIEN habla primero del otro lado.

    UN SOLO SALTO. Si el fragmento siguiente arranca con el CLIENTE, el cliente volvio -- esa
    es una visita nueva por definicion del negocio y la anterior si quedo sin responder. La
    falla real se conserva entera; encadenar hacia atras reconstruiria el stream sin tope que
    SILENCIO_MAX vino a matar.

    Se recorre de atras hacia adelante para que el fragmento ya pegado sea el que el anterior
    mira: asi una cadena no se colapsa sola, porque en cuanto se pega uno el resultado deja
    de empezar con el negocio.
    """
    out: list[list[dict]] = []
    for frag in reversed(partes):
        if out and not _hubo_negocio(frag) and _empieza_el_negocio(out[0]):
            out[0] = frag + out[0]
        else:
            out.insert(0, frag)
    return out


def _solo_cortesia_del_cliente(frag: list[dict]) -> bool:
    """TODOS los mensajes reales del fragmento son del cliente y son cortesia.

    Exige al menos uno: un fragmento sin mensajes reales no es una cola, es ruido de notas.
    Un media suelto NO es cortesia -- un comprobante despues del cierre es un planteo.
    """
    reales = [m for m in frag if not m.get("is_note")]
    if not reales:
        return False
    for m in reales:
        if m.get("from_me"):
            return False
        if (m.get("media_type") or "chat") != "chat":
            return False
        if not _CORTESIA_RE.match(m.get("body") or ""):
            return False
    return True


def _es_cola_de_cortesia(frag: list[dict], previa: list[dict]) -> bool:
    """El fragmento es el "gracias" con el que el cliente cierra la atencion `previa`."""
    if _hubo_negocio(frag) or not _solo_cortesia_del_cliente(frag):
        return False
    # La previa tiene que haber CERRADO: sin cierre no hay cola que pegar, es la misma
    # conversacion siguiendo y de eso ya se ocupa el corte por silencio.
    if not any(es_cierre(m) for m in previa):
        return False
    ultimo_previo = max((m["created_at"] for m in previa if not m.get("is_note")), default=None)
    primero = min((m["created_at"] for m in frag if not m.get("is_note")), default=None)
    if ultimo_previo is None or primero is None:
        return False
    return (primero - ultimo_previo).total_seconds() <= GRACIA_CORTESIA_SEG


def _pegar_colas_de_cortesia(partes: list[list[dict]]) -> list[list[dict]]:
    """Pega hacia ATRAS el "gracias" que quedo del otro lado del cierre.

    Es la regla espejo de `_pegar_continuaciones`: alla la evidencia esta ADELANTE (el
    operador responde en el fragmento siguiente), aca esta ATRAS (el cliente confirma lo que
    el operador ya hizo). Hacia adelante no se puede pegar: el fragmento siguiente arranca
    con el CLIENTE, y ahi la regla del negocio dice visita nueva.

    NO se exige el mismo operador. Si el CRM reasigna el "gracias" a otra persona, sigue
    siendo la cola de la atencion que lo gano -- y era el 12 por ciento de los casos.
    """
    out: list[list[dict]] = []
    for frag in partes:
        if out and _es_cola_de_cortesia(frag, out[-1]):
            out[-1] = out[-1] + frag
        else:
            out.append(frag)
    return out


def partir_en_interacciones(messages: list[dict]) -> list[list[dict]]:
    """Parte el transcript en INTERACCIONES, cortando despues de cada nota de cierre.

    Devuelve los mensajes TAL CUAL vienen (notas incluidas): quien filtra `is_note` es cada
    rubrica, y esta funcion no le saca informacion a nadie.

    Solo devuelve las interacciones que tienen al menos un mensaje REAL: una conversacion
    puede arrancar con "*Asignado automaticamente*" y cerrar sin que nadie hable, y eso no
    es una interaccion. Sin ningun cierre, todo el transcript es UNA interaccion, que es el
    96,3% de los casos.
    """
    ordenados = sorted(messages, key=lambda m: m["created_at"])
    out: list[list[dict]] = []
    actual: list[dict] = []
    cierre_at = None  # cierre visto, esperando a ver si el operador todavia adjunta algo
    # EL SILENCIO SE MIDE ENTRE MENSAJES REALES, y las notas no lo tocan: el ETL las archiva
    # en momentos que no describen la atencion. Es la misma leccion que `_fin_de_actividad`
    # en src/sessions.py, donde una nota corrida al futuro mergeaba dos interacciones
    # genuinamente separadas. Una nota no abre la ventana ni la mantiene viva.
    ultimo_real_at = None
    for i, m in enumerate(ordenados):
        if cierre_at is not None:
            if (m.get("from_me") and not m.get("is_note")
                    and (m["created_at"] - cierre_at).total_seconds() <= GRACIA_CIERRE_SEG):
                actual.append(m)   # mismo gesto: el adjunto es de la interaccion que cierra
                continue
            out.append(actual)     # habla el cliente, o se paso la gracia: corte real
            actual = []
            cierre_at = None
            ultimo_real_at = None
        es_real = not m.get("is_note")
        if (es_real and ultimo_real_at is not None
                and m["created_at"] - ultimo_real_at > SILENCIO_MAX):
            # Nadie hablo en 6 horas: la atencion anterior termino, empieza otra.
            out.append(actual)
            actual = []
            ultimo_real_at = None
        actual.append(m)
        if es_real:
            ultimo_real_at = m["created_at"]
        if es_cierre(m) and not _cierre_rebotado(ordenados, i):
            cierre_at = m["created_at"]
    if actual:
        out.append(actual)
    reales = [i for i in out if any(not m.get("is_note") for m in i)]
    # El pegado va DESPUES del filtro: un fragmento de puras notas no es una atencion y no
    # puede quedar en el medio decidiendo si dos mitades se juntan.
    # Y las colas de cortesia van AL FINAL: primero se arma cada atencion con su respuesta
    # (pegado hacia adelante) y recien despues se le engancha el "gracias" que la cierra.
    # Al reves, el "gracias" podria pegarse a una mitad que todavia no tiene su operador.
    return _pegar_colas_de_cortesia(_pegar_continuaciones(reales))


def tiempos_de(interaccion: list[dict]) -> tuple:
    """Los tres relojes de UNA interaccion: (inicio, primera respuesta del operador, cierre).

    Espejan los campos del CRM (`created_at`, `first_sent_message_at`, `resolved_at`) pero
    a grano INTERACCION. Hacen falta porque los del CRM describen el ENVASE: MEDIDO el
    2026-08-12 sobre el caso `f9b31f4f` (17 interacciones), `created_at` sale de la primera,
    `first_sent_message_at` de la segunda (51,5 h despues) y `assigned_at`/`resolved_at` de
    la ultima -- cuatro interacciones distintas en una sola fila. A nivel poblacion son
    1.208 sesiones de `jugador` (10,2%) con varias interacciones, donde la resolucion
    mostrada pasa de 3,4 h a 88,5-271,3 h y llega a un p90 de 3.834x contra la ventana real.

    Las NOTAS no cuentan para el inicio: la nota de asignacion automatica no es el arranque
    de la atencion. El cierre es la nota `*resuelto*` si esta, y si no el ultimo mensaje
    real. `None` en la primera respuesta cuando nadie contesto -- no se inventa un tiempo.

    El cierre es el ULTIMO `*resuelto*`, no el primero. En UNA interaccion da lo mismo (hay
    uno solo, al final), pero esta funcion tambien se llama con la SESION ENTERA cuando no
    hay ancla determinista -- y ahi tomar el primero de 5 cierres recortaria el tramo a la
    primera interaccion, inventando un tiempo que no describe lo que se juzgo.
    """
    reales = sorted((m for m in interaccion if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    if not reales:
        return (None, None, None)
    inicio = reales[0]["created_at"]
    primera_op = next((m["created_at"] for m in reales if m.get("from_me")), None)
    cierres = [m["created_at"] for m in interaccion if es_cierre(m)]
    return (inicio, primera_op, max(cierres) if cierres else reales[-1]["created_at"])


def interaccion_de(messages: list[dict], ancla: dict) -> list[dict]:
    """La interaccion que CONTIENE `ancla`, para acotar ahi la busqueda de evidencia.

    Es lo que arregla el emparejamiento cruzado: la acreditacion de un comprobante se busca
    en SU interaccion, no en toda la conversacion. Si `ancla` no aparece en ninguna (no
    deberia pasar), devuelve el transcript completo: degradar al comportamiento viejo es
    preferible a devolver vacio y perder la nota.
    """
    for interaccion in partir_en_interacciones(messages):
        if any(m is ancla for m in interaccion):
            return interaccion
    return messages
