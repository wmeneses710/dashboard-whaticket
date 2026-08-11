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


def es_cierre(m: dict) -> bool:
    """El mensaje es la nota interna de cierre del operador."""
    return bool(m.get("is_note")) and bool(_CIERRE_RE.search(m.get("body") or ""))


def partir_en_interacciones(messages: list[dict]) -> list[list[dict]]:
    """Parte el transcript en INTERACCIONES, cortando despues de cada nota de cierre.

    Devuelve los mensajes TAL CUAL vienen (notas incluidas): quien filtra `is_note` es cada
    rubrica, y esta funcion no le saca informacion a nadie.

    Solo devuelve las interacciones que tienen al menos un mensaje REAL: una conversacion
    puede arrancar con "*Asignado automaticamente*" y cerrar sin que nadie hable, y eso no
    es una interaccion. Sin ningun cierre, todo el transcript es UNA interaccion, que es el
    96,3% de los casos.
    """
    out: list[list[dict]] = []
    actual: list[dict] = []
    for m in sorted(messages, key=lambda m: m["created_at"]):
        actual.append(m)
        if es_cierre(m):
            out.append(actual)
            actual = []
    if actual:
        out.append(actual)
    return [i for i in out if any(not m.get("is_note") for m in i)]


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
