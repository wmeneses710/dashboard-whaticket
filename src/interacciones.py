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
    for i, m in enumerate(ordenados):
        if cierre_at is not None:
            if (m.get("from_me") and not m.get("is_note")
                    and (m["created_at"] - cierre_at).total_seconds() <= GRACIA_CIERRE_SEG):
                actual.append(m)   # mismo gesto: el adjunto es de la interaccion que cierra
                continue
            out.append(actual)     # habla el cliente, o se paso la gracia: corte real
            actual = []
            cierre_at = None
        actual.append(m)
        if es_cierre(m) and not _cierre_rebotado(ordenados, i):
            cierre_at = m["created_at"]
    if actual:
        out.append(actual)
    return [i for i in out if any(not m.get("is_note") for m in i)]


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
