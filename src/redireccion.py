"""Traspaso de la conversacion a otra linea nuestra (`redireccion`).

NO es un motivo con nota, es un SKIP condicionado. Definicion del negocio (2026-08-07).

Medido sobre la copia de prod, 671 sesiones de `sistemas` contienen un traspaso y son
tres cosas distintas:
  B  566  el traspaso es UN mensaje dentro de una conversacion real -> ruido; el motivo
          real manda y no se toca nada.
  C   99  el cliente pidio algo concreto y el traspaso fue TODA la respuesta. Estos
          caian en 2 estrellas porque el LLM ve que "no atendio el motivo".
  A    6  el cliente solo dijo "Hola"/"Ok" -> ya los saltea `client_sin_motivo`, y por
          decision del negocio SIGUEN etiquetados `sin_motivo` (no se les cambia).

REGLA PARA C: no lleva nota **solo si** el traspaso apunta a una linea NUESTRA que esta
CONNECTED. El razonamiento: el negocio migro la atencion a otra linea, el operador ya no
puede atender a ese cliente, y ponerle 2 estrellas lo castiga por una decision que no
tomo — la misma logica de `sin_motivo`.
Pero si no hay numero, o la linea esta DISCONNECTED, o no se puede resolver, SI lleva
nota: ahi el cliente queda a la deriva y eso si es mal servicio. El caso real que obligo
a la condicion: "En caso de no responder, contactate con esta linea: 0983744476" apunta a
AGENTES OPERATIVOS PRO, que esta DISCONNECTED.

Sin el mapa de lineas no se skipea NADA (falla del lado seguro): un skip regalado
esconderia sesiones sin atender.
"""
from __future__ import annotations

import re

# Familias de traspaso observadas en la data real (2026-08-07). El rasgo comun es el
# TRASPASO de la conversacion a otro numero o canal, no la mencion de un numero.
#
# OJO CON EL DETECTOR ANCHO. El primer intento (`comunic|escrib|contact...` + digitos)
# daba 6.430 mensajes dominados por plantillas que NO son traspaso:
#   "✨ Gracias por comunicarte con nosotros"           -> 2.121, cierre de cortesia
#   "Mucha suerte hoy... tenemos un numero alterno"     -> despedida
#   "te escribo de Sorti365 para retomar el contacto"   -> prospeccion
# Es la misma trampa que inflo el gate de deposito un 41,4% por leer el script del
# operador: enumerar verbos sueltos captura la plantilla, no la intencion.
TRASPASO_PATTERN = (
    # `atenci[oó]n` ademas de `atend`: "a partir de ahora tu numero principal de ATENCION
    # al Cliente sera" es LA migracion institucional, y `atend` esta en "atenderemos"
    # pero NO en "Atencion". Hallado el 2026-08-12.
    r"a partir de ahora.{0,60}(atend|atenci[oó]n|escrib|comunic)"
    r"|(este|el) (numero|número).{0,40}(fuera de servicio|en revisi)"
    r"|(numeros|números).{0,20}en revisi"
    r"|(escrib|comunic|contact)\w*.{0,25}(al |a la )(siguiente )?(numero|número|linea|línea)"
    r"|te (estaremos|estare|estaré) atendiendo"
    # "seran atendidos directamente por el servicio al cliente de la plataforma".
    # Se exige que el destino sea un SERVICIO y no cualquier articulo: con `por (el|la)`
    # a secas, un inocente "tu recarga fue atendida por el equipo" daba traspaso (lo
    # cazo tests/test_redireccion.py, no la revision a ojo).
    r"|atendid\w+ (directamente )?por (el |la |los |las )?"
    r"(servicio|atencion|atención|plataforma|departamento)"
    r"|contactate con esta (linea|línea)"
    # ACENTOS: `re.IGNORECASE` no los dobla, y esto estaba ASIMETRICO -- la variante
    # `-nos` tenia su forma acentuada y la `-me` no. "Escríbeme al <numero>" es la
    # plantilla de migracion Facebook -> WhatsApp, de las mas frecuentes de la data.
    # El "al" es lo que separa el traspaso del "escríbeme cuando gustes" de la despedida.
    r"|escr[ií]b[ea]me al|escribirme al|escr[ií]b[ae]nos al"
)
_TRASPASO_RE = re.compile(TRASPASO_PATTERN, re.IGNORECASE)

# Candidato a telefono: arranca opcionalmente con +, y admite espacios, guiones y
# parentesis adentro (en la data aparece "+593 99 499 5251").
_TEL_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")

# Cuantos digitos finales identifican una linea. Los mensajes escriben el numero en
# local (`0991194168`) y `connections` lo guarda con pais (`593991194168`): los ultimos
# 9 digitos son la parte que comparten.
TAIL = 9


def es_traspaso(body: str | None) -> bool:
    """El mensaje traspasa la conversacion a otra linea o canal?"""
    return bool(_TRASPASO_RE.search(body or ""))


def tails_del_texto(texto: str | None) -> set[str]:
    """Tails de 9 digitos de todos los telefonos plausibles del texto.

    Una cifra corta (un monto) no puede producir un tail de 9, asi que se descarta sola.
    Un numero que no matchee ninguna linea nuestra simplemente no confirma el traspaso:
    el falso positivo de extraccion no puede regalar un skip.
    """
    out = set()
    for bruto in _TEL_RE.findall(texto or ""):
        digitos = re.sub(r"\D", "", bruto)
        if len(digitos) >= TAIL:
            out.add(digitos[-TAIL:])
    return out


def _mensajes_del_negocio(messages: list[dict]) -> list[dict]:
    """from_me y no nota. Incluye al bot a proposito: si el bot dijo algo que no es
    traspaso, la sesion no es un traspaso puro (criterio conservador)."""
    return [m for m in messages if m.get("from_me") and not m.get("is_note")]


def build_lineas_map(cur, account: str | None = None) -> dict[str, str]:
    """Mapa tail-de-9-digitos -> status, desde `connections`.

    Sin scope de cuenta por default: un traspaso cruza cuentas a proposito (de una linea
    de `sistemas` a la de la plataforma). Ante el mismo numero en varias filas gana
    CONNECTED: si alguna de sus filas esta viva, la linea esta viva.
    """
    where = " WHERE account = %s" if account else ""
    cur.execute(
        "SELECT number, status FROM connections" + where,
        (account,) if account else None,
    )
    lineas: dict[str, str] = {}
    for number, status in cur.fetchall():
        digitos = re.sub(r"\D", "", number or "")
        if len(digitos) < TAIL:
            continue
        tail = digitos[-TAIL:]
        if lineas.get(tail) == "CONNECTED":
            continue
        lineas[tail] = status
    return lineas


def traspaso_a_linea_viva(messages: list[dict], lineas: dict[str, str] | None) -> bool:
    """Algun mensaje de traspaso apunta a una linea NUESTRA que esta CONNECTED?"""
    if not lineas:
        return False
    for m in _mensajes_del_negocio(messages):
        body = m.get("body") or ""
        if not es_traspaso(body):
            continue
        if any(lineas.get(t) == "CONNECTED" for t in tails_del_texto(body)):
            return True
    return False


def es_redireccion_total(messages: list[dict], lineas: dict[str, str] | None) -> bool:
    """La respuesta del negocio fue SOLO un traspaso, y a una linea viva.

    Las dos condiciones juntas son el bucket C skipeable. Si el operador ademas hizo
    otra cosa (bucket B) el motivo real manda; si la linea no esta viva, la sesion se
    evalua igual.
    """
    negocio = _mensajes_del_negocio(messages)
    if not negocio:
        return False
    if not all(es_traspaso(m.get("body")) for m in negocio):
        return False
    return traspaso_a_linea_viva(messages, lineas)
