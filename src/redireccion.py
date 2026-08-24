"""Traspaso de la conversacion a otra linea nuestra (`redireccion`).

ES UN MOTIVO con nota determinista propia (decision del negocio, 2026-08-20). Hasta esa
fecha era un SKIP condicionado (definicion del 2026-08-07): el traspaso puro a una linea viva
se salteaba. El skip protegia bien pero BORRABA el traspaso del tablero -- no se podia contar
ni comparar entre operadores --, asi que la proteccion se movio del gate a la RUBRICA.

Medido sobre la copia de prod, 671 sesiones de `sistemas` contienen un traspaso y son
tres cosas distintas:
  B  566  el traspaso es UN mensaje dentro de una conversacion real -> ruido; el motivo
          real manda y no se toca nada.
  C   99  el cliente pidio algo concreto y el traspaso fue TODA la respuesta. Estos
          caian en 2 estrellas porque el LLM ve que "no atendio el motivo".
  A    6  el cliente solo dijo "Hola"/"Ok" -> ya los saltea `client_sin_motivo`, y por
          decision del negocio SIGUEN etiquetados `sin_motivo` (no se les cambia).

REGLA PARA C (la que da la nota, ver score_redireccion): 4 estrellas -- nunca 5 -- cuando el
cliente TIENE a donde ir; 2 solo cuando quedo a la deriva y podemos PROBARLO. El razonamiento
del techo: el negocio migro la atencion, el operador ya no puede atender a ese cliente, y
castigarlo seria por una decision que no tomo (misma logica que `sin_motivo`); el de no darle
5: al cliente no se le resolvio nada, todavia tiene que escribir a otro lado.

LA PRUEBA SE EXIGE, NO SE PRESUME, y esto se midio el 2026-08-20. La primera version bajaba a
2 estrellas cuando el numero no aparecia en el mapa de lineas, y de 273 sesiones **270 eran
falsas acusaciones**: 238 tenian un numero que no esta en `connections` (que trae `number`
NULL en casi todas sus filas, asi que el mapa conoce apenas 9 lineas -- y tres tails
concentraban 230 de esas 238, o sea lineas reales sin registrar) y 32 derivaban a un wa.link,
que es un destino perfectamente valido. Solo 3 eran el caso real: numero NUESTRO y
DISCONNECTED, como "contactate con esta linea: 0983744476" -> AGENTES OPERATIVOS PRO.

Es el mismo criterio con el que este modulo nacio -- "sin el mapa de lineas no se skipea NADA,
falla del lado seguro" -- ahora aplicado a la NOTA: para bajarsela a un operador hay que
probar que dejo al cliente sin atencion, no simplemente no poder confirmar lo contrario.
"""
from __future__ import annotations

import re

from src.catalogo_coaching import consejo_de
from src.scorer import ScoreResult

# Misma convencion que las otras rubricas deterministas (ver src/info.py): el `llm_model`
# declara QUIEN puso la nota, y aca no la puso ningun modelo.
MODELO_DETERMINISTA = "determinista/redireccion-v1"

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


def tail_de(numero: str | None) -> str | None:
    """Los ultimos `TAIL` digitos de un numero, o None si no da para una linea.

    `connections` guarda el numero con pais (`593959803754`) y los mensajes lo escriben en
    local (`0959803754`) o con separadores (`+593 959 803 754`): los ultimos 9 digitos son la
    parte que comparten, y es la misma normalizacion que hace `tails_del_texto` del lado del
    texto. Tenerla en una funcion evita que las dos puntas de la comparacion se separen.
    """
    digitos = re.sub(r"\D", "", numero or "")
    return digitos[-TAIL:] if len(digitos) >= TAIL else None


def es_traspaso(body: str | None, lineas: dict[str, str] | None = None,
                linea_propia: str | None = None) -> bool:
    """El mensaje traspasa la conversacion a otra linea o canal?

    DOS CAMINOS, y el segundo entro el 2026-08-24 porque el primero solo no alcanzaba.
      1. LA FRASE (`TRASPASO_PATTERN`): cubre lo que no trae numero -- los wa.link y las
         migraciones de canal.
      2. EL DESTINO: el mensaje nombra una linea NUESTRA, CONNECTED y DISTINTA de aquella en
         la que esta el chat. No hace falta ningun verbo.

    POR QUE LA FRASE SOLA NO ALCANZA. Caso real: "0959803754 este es mi numero ahora amigo"
    -- `ONLY 2`, linea nuestra y viva. `tails_del_texto` lo extraia y el mapa lo tenia, pero
    la regex pedia un verbo y devolvia False, asi que el numero no se llegaba a mirar.
    Y la frase NO discrimina: medidos 41 mensajes con "comuniquese ... agente" + numero,
    **28 apuntan a una linea NUESTRA y 13 a un numero AJENO** -- redireccion y derivacion,
    opuestas, con la MISMA redaccion. La regex acertaba y fallaba en los dos grupos por
    igual: castigaba derivaciones legitimas y perdia redirecciones.

    EL DESTINO SI DISCRIMINA, y esta medido: de los 143 mensajes que este camino agrega,
    **cero falsos positivos claros** sobre los 122 textos distintos leidos uno por uno. Un
    operador no tiene motivo para tipear el numero de OTRA linea de la empresa salvo para
    mandar al cliente ahi. Los mas frecuentes son justamente los que la regex jamas podria
    cazar: el numero SOLO, sin una palabra ("+593991194133", 10 veces).

    LA LINEA PROPIA SE EXCLUYE. "Te paso mi numero <la misma linea>" es una DESPEDIDA (38
    medidas en 45 dias), y el template mas frecuente del corpus es exactamente eso ("Estoy a
    la orden siempre. Escribeme de una cuando gustes...", 2.505 veces).

    UNA LINEA CAIDA NO CUENTA: mandar a una linea muerta no es traspasar, es dejar al cliente
    sin a donde ir, y la rubrica lo juzga aparte (`destino_probadamente_caido`).

    SIN MAPA NO SE INVENTA NADA. Y sin `linea_propia` (Facebook, Instagram y Telegram guardan
    `connections.number = NULL`) se compara igual contra el mapa: lo unico que se pierde es
    poder descartar la despedida, y ahi el canal ya es distinto por definicion.
    """
    if _TRASPASO_RE.search(body or ""):
        return True
    if not lineas:
        return False
    destinos = tails_del_texto(body) - ({linea_propia} if linea_propia else set())
    return any(lineas.get(t) == "CONNECTED" for t in destinos)


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


def traspaso_a_linea_viva(messages: list[dict], lineas: dict[str, str] | None,
                          linea_propia: str | None = None) -> bool:
    """Algun mensaje de traspaso apunta a una linea NUESTRA que esta CONNECTED?"""
    if not lineas:
        return False
    for m in _mensajes_del_negocio(messages):
        body = m.get("body") or ""
        if not es_traspaso(body, lineas, linea_propia):
            continue
        if any(lineas.get(t) == "CONNECTED" for t in tails_del_texto(body)):
            return True
    return False


# Link de WhatsApp: un destino tan valido como un numero, y MUY usado ("escribenos al
# siguiente enlace: https://wa.link/..."). MEDIDO el 2026-08-20: 32 de las 273 sesiones que
# la primera version de la rubrica iba a castigar por "sin numero" tenian un wa.link. Buscar
# solo digitos las leia como si el cliente quedara sin a donde ir.
_LINK_RE = re.compile(r"(wa\.link/|wa\.me/|api\.whatsapp\.com/)", re.IGNORECASE)


def _mensajes_de_traspaso(messages: list[dict], lineas: dict[str, str] | None = None,
                          linea_propia: str | None = None) -> list[dict]:
    return [m for m in _mensajes_del_negocio(messages)
            if es_traspaso(m.get("body"), lineas, linea_propia)]


def tiene_destino(messages: list[dict], lineas: dict[str, str] | None = None,
                  linea_propia: str | None = None) -> bool:
    """El traspaso le dice al cliente A DONDE ir: un telefono o un link de WhatsApp.

    Sin destino el cliente queda literalmente sin salida ("te vamos a atender por otro
    canal" y nada mas), y eso si es mal servicio probado.
    """
    for m in _mensajes_de_traspaso(messages, lineas, linea_propia):
        body = m.get("body") or ""
        if tails_del_texto(body) or _LINK_RE.search(body):
            return True
    return False


def destino_probadamente_caido(
    messages: list[dict], lineas: dict[str, str] | None,
    linea_propia: str | None = None,
) -> bool:
    """El destino es una linea NUESTRA que sabemos CAIDA. Exige prueba, no ausencia.

    NO ALCANZA con "el numero no esta en el mapa". `connections.number` viene NULL en casi
    todas sus filas, asi que el mapa conoce apenas 9 lineas: medido el 2026-08-20, 238 de
    273 sesiones tenian un numero desconocido, y tres tails concentraban 230 de ellas --
    lineas reales del negocio sin registrar. Tratar "no lo conozco" como "esta muerto"
    fabricaba 238 acusaciones.

    Es el mismo criterio con el que este modulo ya nacio ("sin el mapa de lineas no se
    skipea NADA, falla del lado seguro"), aplicado ahora a la nota: para bajarle la nota a
    un operador hay que PROBAR que dejo al cliente sin atencion.
    """
    if not lineas:
        return False
    for m in _mensajes_de_traspaso(messages, lineas, linea_propia):
        tails = tails_del_texto(m.get("body") or "")
        conocidos = [lineas[t] for t in tails if t in lineas]
        # Si alguna linea conocida del mensaje esta viva, el destino sirve.
        if any(st == "CONNECTED" for st in conocidos):
            return False
        if conocidos:
            return True
    return False


def respuesta_fue_solo_traspaso(messages: list[dict],
                                lineas: dict[str, str] | None = None,
                                linea_propia: str | None = None) -> bool:
    """TODA la respuesta del negocio fue traspaso, sin mirar a donde apunta.

    Es la mitad del bucket C que NO depende del mapa de lineas, y es la que decide el
    MOTIVO: si el operador ademas atendio (bucket B), manda el motivo real. A donde lo
    mandaron decide la NOTA, no el motivo -- son dos preguntas y antes estaban pegadas en
    `es_redireccion_total`, que exigia la linea viva y por eso dejaba el caso "lo mando a
    una linea caida" sin motivo propio: caia al LLM y ahi era una sesion cualquiera.
    """
    negocio = _mensajes_del_negocio(messages)
    if not negocio:
        return False
    return all(es_traspaso(m.get("body"), lineas, linea_propia) for m in negocio)


def es_redireccion_total(messages: list[dict], lineas: dict[str, str] | None) -> bool:
    """La respuesta del negocio fue SOLO un traspaso, y a una linea viva.

    SE MANTIENE por compatibilidad de la lectura vieja (traspaso puro + linea viva), pero
    ya NO decide un skip: desde el 2026-08-20 `redireccion` es un motivo con nota propia.
    """
    return (respuesta_fue_solo_traspaso(messages)
            and traspaso_a_linea_viva(messages, lineas))


def traspaso_limpio(messages: list[dict], lineas: dict[str, str] | None,
                    linea_propia: str | None = None) -> bool:
    """El traspaso fue PURO y a una linea nuestra que esta viva -> no hay nada que calificar.

    DECISION DEL NEGOCIO (2026-08-24): "si es redireccion no deberia ni calificarse, porque es
    algo que no le compete, y la mayoria ni explica, seria simplemente redireccionar y ya".
    MEDIDO sobre 2.500 sesiones: 13 son traspaso puro y **12 daban 4 estrellas**. Una nota que
    califica igual al 92% no mide nada, y los textos son plantillas.

    NO CUBRE EL TRASPASO SIN DESTINO ni el que apunta a una linea CAIDA. Eso sale del mismo
    argumento del negocio: "no le compete" vale para mandarlo a una linea viva; elegir mandarlo
    a la nada SI le compete, y el cliente queda sin a donde escribir. Ese caso conserva sus 2
    estrellas (`score_redireccion`).

    SIN MAPA devuelve False: sin poder PROBAR que el destino esta vivo no se saltea nada. Es la
    misma regla que ya rige en `destino_probadamente_caido` -- se exige prueba, no ausencia.
    """
    if not lineas or not respuesta_fue_solo_traspaso(messages, lineas, linea_propia):
        return False
    return (tiene_destino(messages, lineas, linea_propia)
            and not destino_probadamente_caido(messages, lineas, linea_propia))


def score_redireccion(
    messages: list[dict], lineas: dict[str, str] | None,
    linea_propia: str | None = None,
) -> ScoreResult | None:
    """La nota del traspaso puro. SIN LLM. None si no es traspaso puro (cede el turno).

    EL EJE ES EL DEL MANUAL DE ATC, no la data. E07 ("transferir un chat sin notificar al
    cliente... el cliente debe saber que otro operador continuara su atencion") y su espejo
    B09 ("informar al cliente cuando su caso sera transferido"). En un traspaso puro el
    aviso EXISTE por construccion -- el mensaje de traspaso ES el aviso --, asi que B09 se
    cumple siempre y no discrimina. Lo que discrimina es A DONDE lo mandan.

    TECHO EN 'buena' (4), NUNCA 5. El operador cumplio una migracion que decidio el negocio
    y le aviso al cliente, asi que no es una falla -- pero al cliente no se le resolvio nada:
    todavia tiene que escribir a otro lado. Un 5 diria que la atencion fue excelente y lo
    que hubo fue una derivacion correcta.

    2 ESTRELLAS CUANDO QUEDA A LA DERIVA. Sin numero resoluble, o a una linea DISCONNECTED,
    el cliente se queda sin a donde ir. Es el caso real que obligo a la condicion original:
    "En caso de no responder, contactate con esta linea: 0983744476" apuntaba a AGENTES
    OPERATIVOS PRO, que estaba DISCONNECTED.
    """
    # EL MISMO CRITERIO QUE EL RUTEO, o las dos puntas se separan. El worker rutea con
    # `respuesta_fue_solo_traspaso` y aca se re-chequea: si una ve el destino y la otra solo
    # la frase, el ruteo manda la sesion a `redireccion`, esta funcion devuelve None y en esa
    # rama del worker no hay fallback -> la sesion se queda SIN NOTA. Detectado sobre la
    # sesion real `9813f9a2` mientras se implementaba el destino (2026-08-24).
    if not respuesta_fue_solo_traspaso(messages, lineas, linea_propia):
        return None
    # LAS TRES PREGUNTAS SE HACEN CON EL MISMO CRITERIO. Si `tiene_destino` sigue mirando
    # solo la frase mientras el ruteo mira el destino, encuentra CERO mensajes de traspaso y
    # la rubrica concluye "lo derivó sin dejarle una línea" sobre un operador que SI dejo una
    # linea viva: una acusacion falsa, y nueva. Ver tests/test_redireccion_por_destino.py.
    viva = (tiene_destino(messages, lineas, linea_propia)
            and not destino_probadamente_caido(messages, lineas, linea_propia))
    if viva:
        stars, label = 4, "buena"
        rationale = ("El operador derivó al cliente a otra línea nuestra que está activa. "
                     "Le avisó del traspaso, pero la solicitud no se atendió acá.")
        consejo = None
    else:
        stars, label = 2, "deficiente"
        rationale = ("El operador derivó al cliente sin dejarle una línea a la que "
                     "escribir: el número no se pudo resolver o la línea está caída.")
        # EL CONSEJO VIVE EN EL CATALOGO (C42 -> B09). Estaba asignado aca adentro, que es
        # por que el guard de `_COACHING` no lo veia. Migrado VERBATIM el 2026-08-24: no
        # cambia ninguna nota, le pone codigo y practica a lo que ya se decia.
        consejo = consejo_de("redireccion", "deficiente")
    return ScoreResult(
        rubric="redireccion",
        motivo="redireccion",
        rating_label=label,
        stars=stars,
        rating_rationale=rationale,
        # `destino_utilizable` y NO `traspaso_a_linea_viva`: el valor ya no dice "la linea
        # esta CONNECTED en el mapa" (imposible de saber, el mapa conoce 9 lineas) sino "el
        # cliente tiene a donde ir y no probamos que este caido". El nombre viejo mentiria
        # en el tablero justo en las 238 sesiones cuyo numero no esta registrado.
        dimensions={"destino_utilizable": viva},
        llm_model=MODELO_DETERMINISTA,
        # Sin uplift ni observacion de deposito: en un traspaso no hay conversion que
        # empujar ni transaccion que observar.
        atencion=None,
        deposit_observed=None,
        floor_applied=False,
        recomendacion=consejo.texto if consejo else "",
        recomendacion_codigos=[consejo.codigo] if consejo else [],
        recomendacion_practica=consejo.practica if consejo else "",
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
