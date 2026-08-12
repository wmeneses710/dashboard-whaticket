"""Senales deterministas de RESOLUCION del operador (capa sin LLM).

Corrige la dureza sistematica del modelo detectada en la auditoria: el LLM hunde
por debajo del piso interacciones donde el operador SI atendio el motivo, porque
(a) confirmo la transaccion con una plantilla ("ing"/"listo"/"saldo disponible"),
(b) mando el comprobante/tutorial como media que el modelo no puede leer, o
(c) el cliente abandono despues de una respuesta accionable.

Estas funciones puras dan la evidencia determinista para que el scorer aplique un
PISO (nunca sube a buena/excelente; solo evita el deficiente/mala injusto) y para
que el router no saltee un deposito estandar como 'customer_media_only'.

Mensajes = dicts con: from_me, is_note, body, media_type, sent_from.
Se evalua SOLO al operador HUMANO (from_me, no nota, sent_from != CHATBOT).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from src.deposits import RECHARGE_PATTERN
from src.metrics import _is_bot

# Confirmacion transaccional del operador. Tokens reales del dataset (plantillas y
# taquigrafia de operador): "ing"/"ingreso"/"ingresado", "acreditado", "cargado",
# "realizado/procesado/reflejado/abonado", "listo", "en breve", "disponible"
# (saldo disponible). Deliberadamente SIN tokens genericos ("hecho") para no
# floorear conversaciones que no son una confirmacion. Se aplica solo a motivos
# transaccionales, asi que dentro de ese contexto estos tokens son confirmaciones.
CONFIRMATION_PATTERN = (
    r"\b(ing|ingr|ingres[oó]?|ingresad[oa]s?|acredit\w*|cargad[oa]s?|carg[oó]|"
    r"realizad[oa]s?|procesad[oa]s?|reflejad[oa]s?|abonad[oa]s?|listo|en breve|disponible)\b"
)
_CONFIRMATION_RE = re.compile(CONFIRMATION_PATTERN, re.IGNORECASE)


# --- ACUSE vs ACREDITACION ---------------------------------------------------
# `operator_confirmation` (arriba) mezcla las dos: "en breve" (voy) y "acreditado"
# (llego). La rubrica de deposito las necesita separadas, porque su 2 estrellas es
# exactamente "acuso pero nunca confirmo la acreditacion".
#
# Medido sobre 1.254 transacciones de deposito (1 por persona, jul-ago 2026):
# contando solo plantillas la falla daba 42,4%; sumando toda la taquigrafia daba
# 28,1%. Ninguno sirve. El primero SUBCUENTA (los operadores confirman con
# taquigrafia: `listo` 409 veces, `cargado/cargo` 98, `disponible` 36). El segundo
# SOBRECUENTA por polisemia; falsos positivos verificados leyendo los mensajes:
#   "la app aun no esta DISPONIBLE"                  -> habla de la app
#   "Registro o INGRESO con los datos que le pase"   -> ingreso = iniciar sesion
#   "LISTO, enviame tu usuario para revisar..."      -> listo = "ok", sigue pidiendo

# Tokens que solo significan que la plata se movio. No hay lectura alternativa.
_ACREDITA_FUERTE_RE = re.compile(
    # OJO con 'reflej': va la forma consumada (reflejado/reflejo) pero NO el futuro
    # "se reflejara en breve", que es ACUSE. Por eso no se usa \w* aca.
    # `cargu[eé]`/`acredit` en PRIMERA PERSONA: "ya te lo cargué" se escapaba porque
    # `carg[oó]` agarra "cargo"/"cargó" y no la forma con -ué. Se exige el "ya" delante para
    # no morder el subjuntivo ("para que se cargue", que es pendiente, no hecho).
    # `acredit(?!ar)`: el INFINITIVO no es una acreditacion -- "para acreditar necesito el
    # comprobante" es el operador PIDIENDO (183 mensajes), y "voy a acreditar" es intencion.
    r"\b(acredit(?!ar\b)\w*|abonad[oa]s?|abon[oó]|reflejad[oa]s?|reflej[oó]|"
    r"cargad[oa]s?|carg[oó]|ingresad[oa]s?|ingres[óo]s?\b(?!\s+con)|ing|ingr)\b"
    r"|ya\s+((te|le|se)\s+)?((lo|la)\s+)?(cargu[eé]|acredit[eé])\b",
    re.IGNORECASE)
# La OPERACION consumada, en pasado. "ya se proceso" (hecho) es distinto de "esta siendo
# procesada" (en curso), que es el ACUSE y sigue afuera via _ACUSE_RE. El unico falso
# positivo del dataset son 3 mensajes donde lo realizado es el TRAMITE y no la plata
# ("su verificacion ya esta realizada"), y se excluyen por el sujeto.
_ACREDITA_HECHO_RE = re.compile(
    r"ya\s+(se\s+)?(est[aá]|qued[oó])?\s*(realizad[oa]|proces[oó]|realiz[oó])\b",
    re.IGNORECASE)
_TRAMITE_RE = re.compile(
    r"\b(verificaci[oó]n|solicitud|registro|cuenta|documento)\b[^.!?\n]{0,30}"
    r"ya\s+(est[aá]|qued[oó])", re.IGNORECASE)
# 'disponible' solo vale si habla del SALDO, no de la app ni de una promo.
# 'ya puedes usar/disfrutar TU SALDO' es la misma idea que 'tu saldo ya esta disponible':
# la plata esta ahi y el cliente puede tocarla. Exige el saldo por la misma razon que
# 'disponible' lo exige -- "ya puedes usar la app" o "ya puedes disfrutar de todas las
# promociones" no acreditan nada.
# 'el saldo YA ESTA EN TU CUENTA' es la misma idea con otras palabras, y es texto LIBRE del
# operador (76 conversaciones). Se exige el verbo (`esta`/`se encuentra`/`lo tienes`) para no
# morder "para retirar el saldo de tu cuenta", que habla de sacarla, no de que llego.
_ACREDITA_SALDO_RE = re.compile(
    r"(saldo\w*[^.!?\n]{0,40}disponible|disponible[^.!?\n]{0,25}saldo|"
    r"recarga (exitosa|acreditada|realizada)|gracias por tu recarga|"
    r"ya (puedes|puede) (disfrutar|usar|utilizar) (de )?(tu|su) saldo|"
    r"saldo\w*[^.!?\n]{0,25}(est[aá]|se encuentra)[^.!?\n]{0,12}en (tu|su) cuenta|"
    r"ya (lo|la) (tienes|tiene)[^.!?\n]{0,15}en (tu|su) cuenta)",
    re.IGNORECASE)
# Un "listo" seco confirma; un "listo" seguido de otra instruccion, no.
_LISTO_RE = re.compile(r"^\s*listo\b", re.IGNORECASE)
# La negacion invalida la frase entera.
_NEGACION_RE = re.compile(
    r"\b(no|aun no|a[uú]n no|todav[ií]a no|nunca)\b", re.IGNORECASE)
# La PROMESA A FUTURO tampoco es una acreditacion: es el ACUSE. Espeja el cuidado que ya
# se tuvo con 'reflej' (la forma consumada si, el futuro no), que quedo a medias porque
# "En breve tendras tu saldo disponible" -la plantilla de acuse de MAYOR volumen del
# dataset- si matcheaba _ACREDITA_SALDO_RE por el "saldo ... disponible". Medido el
# 2026-08-11: 103 sesiones de `deposito` prometen y nunca confirman, y cobraban 3,89
# estrellas cuando su nota es 2 ("nunca le confirmo que la plata habia entrado").
_FUTURO_RE = re.compile(
    # `ahorita`/`ahora mismo`: el futuro inmediato mas ecuatoriano de todos, y faltaba.
    r"\b(en breve|en un momento|en unos minutos|ya mismo|enseguida|en seguida"
    r"|ahorita|ahora mismo)\b",
    re.IGNORECASE)

# LA PROMESA EN PRIMERA PERSONA. "Ya le cargo" es "yo lo cargo, ahora", no "esta cargado", y
# `_strip_accents` corre ANTES del match: borra el acento que era la UNICA señal entre "cargó"
# (hecho) y "cargo" (lo hago). El patron viejo asumia que "cargo" era taquigrafia de "cargado".
#
# LO DECIDE EL PRONOMBRE, y lo confirma la data (2026-08-12, vara = las 77.005 formas en
# pasado, que tienen una confirmacion posterior el 20% de las veces):
#   `ya LE/TE cargo`   1a persona    441 msjs   59% confirma DESPUES  -> PROMESA
#   `ya SE cargo`      = "se cargó"   47 msjs   11% confirma despues  -> HECHO
# O sea: los operadores mismos tratan la promesa y la confirmacion como dos actos distintos.
# `se lo/la cargo` tambien es primera persona (dativo + acusativo); `se cargo` a secas, no.
#
# El caso que lo destapo mostraba «✓ le confirmó que el saldo ya estaba acreditado» cuando el
# unico mensaje era "Ya le cargo mi amigo". Una tilde FALSA es peor que una nota baja: afirma
# al negocio que hubo una confirmacion que nunca existio.
_PROMESA_1A_RE = re.compile(
    r"\bya\s+(le|te|les)\s+(cargo|acredito|abono|ingreso)\b"
    r"|\bya\s+se\s+(lo|la)\s+(cargo|acredito|abono)\b",
    re.IGNORECASE)

# ACUSE: el operador avisa que esta en eso. NO es que llego.
ACUSE_PATTERN = (
    r"estamos (verificando|procesando|revisando)|se reflejar[aá] en breve|"
    r"est[aá] siendo procesad|en proceso|en breve|"
    r"permitame un momento|perm[ií]tame un momento|dame un momento|un momento por favor|"
    r"ya (mismo )?(lo|la) (proceso|reviso|verifico)"
)
_ACUSE_RE = re.compile(ACUSE_PATTERN, re.IGNORECASE)


def _frases(body: str) -> list[str]:
    """Corta por fin de oracion, NO por coma.

    La coma es justamente donde vive el falso positivo: "Listo, enviame tu usuario"
    sigue siendo una sola idea y no confirma nada.
    """
    return [f.strip() for f in re.split(r"[.!?\n]+", body or "") if f.strip()]


def operator_acreditacion(messages: list[dict]) -> bool:
    """True si el OPERADOR confirmo que la plata LLEGO (no que la esta procesando)."""
    for m in messages:
        if not _is_operator(m):
            continue
        for frase in _frases(m.get("body") or ""):
            if _NEGACION_RE.search(frase) or _FUTURO_RE.search(frase):
                continue
            sin_acentos = _strip_accents(frase)
            if _PROMESA_1A_RE.search(sin_acentos):
                continue
            if _ACREDITA_FUERTE_RE.search(sin_acentos):
                return True
            if _ACREDITA_SALDO_RE.search(sin_acentos):
                return True
            # La operacion consumada, salvo que lo realizado sea el TRAMITE y no la plata.
            if (_ACREDITA_HECHO_RE.search(sin_acentos)
                    and not _TRAMITE_RE.search(sin_acentos)):
                return True
            # "Listo amiga" confirma; "Listo, enviame tu usuario para..." no.
            if _LISTO_RE.match(frase) and len(frase.split()) <= 3:
                return True
    return False


# El operador CHEQUEA si falta algo antes de cerrar. Distinto de la plantilla de
# despedida ("Gracias por preferirnos"), que se despide sin ofrecer nada. Linea base
# medida el 2026-08-06: 13,0% de las sesiones lo hacen, con una varianza enorme entre
# operadores (Mario 59 de 89 = 66%; Andree Rodriguez 0 de 112). No es una conducta
# aspiracional: ya existe en la operacion y se puede enseñar.
ANYTHING_ELSE_PATTERN = (
    r"algo mas|alguna otra (duda|consulta|solicitud|pregunta|cosa)|otra duda|"
    r"en que mas (te|le) (puedo|podemos) ayudar|necesitas? algo|necesita algo|"
    r"te (puedo|podemos) ayudar en algo mas|alguna inquietud|"
    r"(te )?qued[oó] alguna (duda|inquietud)|te ayudo en algo mas"
)
_ANYTHING_ELSE_RE = re.compile(ANYTHING_ELSE_PATTERN, re.IGNORECASE)


# El DOMINIO de la recarga tiene una sola fuente: src.deposits.RECHARGE_PATTERN, el mismo
# que usan el gate en Python y la agregacion en SQL. Aca se reusa para exigir que el acuse
# hable de una RECARGA y no de cualquier cosa.
_RECARGA_DOMINIO_RE = re.compile(RECHARGE_PATTERN, re.IGNORECASE)
# El operador PIDE el comprobante en vez de reconocerlo recibido -> la imagen que llego
# no era el comprobante. Solo formas IMPERATIVAS/de pedido: el acuse real "gracias por
# enviarme tu comprobante" NO cae aca, porque el infinitivo 'enviarme' no matchea el
# imperativo 'enviame'.
_PIDE_COMPROBANTE_RE = re.compile(
    r"(env[ií]ame|env[ií]eme|m[aá]ndame|m[aá]ndeme|adj[uú]nta|adj[uú]nte|"
    r"necesito que|hace falta que)",
    re.IGNORECASE)


def operator_acuso_comprobante(messages: list[dict], desde=None) -> bool:
    """El OPERADOR reconocio un comprobante de recarga RECIBIDO (si `desde`, posterior a el).

    Corrobora QUE LA IMAGEN ERA UN COMPROBANTE, no que el operador lo hizo bien. Es la
    unica corroboracion posible cuando el cliente manda la imagen y NO ESCRIBE NADA, que
    es el caso modal: en la copia de prod el caption de 33.914 comprobantes es vacio y el
    de otros 11.270 lo pone la app del banco. Sin esto, 5.521 depositos con comprobante
    (99,96% de los que caian al LLM) quedaban fuera de su rubrica determinista.

    Exige las DOS cosas en el MISMO mensaje:
      - dominio de recarga, que descarta un acuse generico ("permitame un momento")
        despues de cualquier foto;
      - acuse o acreditacion, que descarta el pitch de venta ("con tu primera carga...")
        donde el operador habla del dominio sin reconocer nada recibido.
    Y descarta el PEDIDO del comprobante, que cumple las dos pero prueba lo contrario.

    Vale el ACUSE ("en breve") y no solo la acreditacion, A PROPOSITO: si se exigiera que
    la plata llego, solo los depositos BIEN atendidos entrarian a la rubrica y los mal
    atendidos seguirian yendose al pase con LLM, que los califica mas alto. La señal es
    del ARTEFACTO, no de la CALIDAD.
    """
    for m in messages:
        if not _is_operator(m):
            continue
        if desde is not None:
            creado = m.get("created_at")
            if creado is None or creado <= desde:
                continue
        texto = _strip_accents(m.get("body") or "")
        if not _RECARGA_DOMINIO_RE.search(texto) or _PIDE_COMPROBANTE_RE.search(texto):
            continue
        if _ACUSE_RE.search(texto) or operator_acreditacion([m]):
            return True
    return False


# CORTESIA / ACUSE / SALUDO: el bloque no pide nada. Se evalua sobre el texto COMPLETO
# del bloque, no sobre su primer mensaje: el agente suele mandar "gracias" y una imagen
# en el mismo bloque, y mirar solo el primero clasificaba mal (medido: da 46% de falsas
# cortesias cuando el numero real es 1,4%).
#
# Se decide por VOCABULARIO y no por un regex de alternativas. El regex anterior
# matcheaba UNA alternativa y despues exigia fin de string, asi que la cortesia
# COMPUESTA — que es como habla el agente de verdad — se le escapaba entera:
# "hola buenas noches", "ok muy bien", "listo gracias", "no muchas gracias". Todas
# contaban como un pedido sin responder. Tambien se le escapaban tokens del dataset
# que no estaban en la lista ("tks", "bueno", "buen dia", "muy amable") y cualquier
# emoji fuera de la clase fija que traia ("☺️", "🫂").
#
# La regla es conservadora POR DISEÑO: el bloque es cortesia solo si TODAS sus palabras
# estan en el vocabulario. Alcanza UNA palabra de verdad ("comision", "por fa",
# "me avisa") para que vuelva a exigir respuesta.
_CORTESIA_VOCAB = frozenset({
    # acuse
    "ok", "oka", "okay", "okey", "oki", "dale", "listo", "lista", "vale", "entendido",
    "de", "acuerdo", "correcto", "ya", "esta", "voy", "pedi", "va",
    # agradecimiento
    "gracias", "gracia", "muchas", "mil", "tks", "thanks", "thank", "you",
    # valoracion
    "bien", "muy", "buenisimo", "perfecto", "excelente", "bendiciones", "genial",
    "amable", "bueno", "buena",
    # saludo / despedida
    "hola", "buenas", "buenos", "buen", "dia", "dias", "tardes", "noches",
    # respuestas minimas
    "si", "no", "claro",
})

# Todo lo que no es palabra ni espacio (puntuacion Y emojis) se descarta antes de
# comparar contra el vocabulario.
_NO_PALABRA_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _palabras_normalizadas(texto: str) -> list[str]:
    """Palabras en minuscula, sin tildes, sin puntuacion ni emojis."""
    plano = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return [p for p in _NO_PALABRA_RE.sub(" ", plano).split() if p]


# El opener que genera el WIDGET de la web, no la persona. Medido el 2026-08-11: 2.385
# sesiones del segmento agente (4,5%) arrancan con esta frase y el 100% de ellas NO tiene
# ningun media real en toda la sesion — nunca hay comprobante ni transaccion. Se median
# igual como pedido de caja: en un caso, 7 segundos por encima del umbral bajaron la sesion
# entera a 3 estrellas por una frase que no pide nada.
_OPENER_WIDGET_RE = re.compile(
    r"^\W*hola\W*(estoy escribiendo|te escribo|escribo)\s+desde\s+sorti\.?\s?ec\W*$",
    re.IGNORECASE)


def es_cortesia(texto: str) -> bool:
    """El texto es puro saludo/acuse/agradecimiento, sin pedir nada?

    Vacio NO es cortesia: es ausencia de texto, y quien decide en ese caso es
    `es_pedido` (un bloque vacio sin adjunto no pide nada).

    PERO un texto que SI tiene contenido y al normalizar no deja NINGUNA palabra -puro
    emoji o puntuacion- si es cortesia. Antes caia en el `bool(palabras)` y daba False,
    al revés de la intencion: `es_pedido` lo tomaba como pedido real y la sesion sacaba
    1 estrella por un cierre agradecido que nadie tenia que contestar (casos reales
    `6315c196` cerrando con "🙌🏻" y `822f5cb4` con "🫱🏼‍🫲🏼").
    """
    texto = (texto or "").strip()
    if not texto:
        return False
    if _OPENER_WIDGET_RE.match(texto):
        return True
    palabras = _palabras_normalizadas(texto)
    if not palabras:
        return True
    return all(p in _CORTESIA_VOCAB for p in palabras)


def client_sin_motivo(messages: list[dict]) -> bool:
    """El cliente nunca planteo NADA: todo lo suyo es saludo, acuse o agradecimiento.

    Es el motivo `sin motivo` que definio el negocio ("hola y se fue"). No se
    califica: poner nota a una conversacion donde el cliente no pidio nada es
    calificar al operador por una prospeccion que no prendio.

    SE DETECTA POR LO CERRADO, no por lo abierto. Enumerar "que cuenta como plantear
    algo" es una lista infinita y siempre se escapan casos — el primer intento marcaba
    como sin-motivo a "buenas mandeme una cuenta pichincha", "mas informacion por
    favor" y "de q de trata", que son pedidos y preguntas de verdad. El vocabulario de
    cortesia SI es un conjunto cerrado: alcanza una sola palabra desconocida para que
    la sesion vuelva a ser calificable. Falla del lado seguro.

    Un adjunto del cliente TAMPOCO es sin-motivo aunque el texto sea cortesia: mandar
    un comprobante es plantear algo.

    Medido (2026-08-06, 1 sesion por persona): 42 de 1.008 sesiones de jugador (4,2%),
    concentradas en `registro` (8,7%), que es donde vive la prospeccion saliente.
    """
    cliente = [m for m in messages if not m.get("from_me") and not m.get("is_note")]
    if not cliente:
        return False   # sin cliente decide `decide_eligibility`, no esta funcion
    if any(is_real_media(m.get("media_type")) for m in cliente):
        return False
    texto = " ".join(
        " ".join((m.get("body") or "").split()) for m in cliente
    ).strip()
    return bool(texto) and es_cortesia(texto)


def tiene_reloj(messages: list[dict]) -> bool:
    """Todos los mensajes traen `created_at`?

    `fetch_messages` (path por conversacion) NO lo trae; solo lo trae
    `fetch_session_messages`. Es la trampa documentada en src/context.py, que ya
    rompio la rubrica de agilidad recien contra la BD y volvio a aparecer con la de
    deposito. Cualquier rubrica que mida tiempos chequea esto primero y cede el turno
    en vez de explotar con KeyError.
    """
    return all(m.get("created_at") is not None
               for m in messages if not m.get("is_note"))


def _is_operator(m: dict) -> bool:
    """Operador humano: enviado por el negocio (from_me), no nota, no bot."""
    return bool(m.get("from_me")) and not m.get("is_note") and not _is_bot(m)


def operator_confirmation(messages: list[dict]) -> bool:
    """True si algun mensaje del OPERADOR confirma la transaccion (token de plantilla)."""
    return any(
        _CONFIRMATION_RE.search(m.get("body") or "")
        for m in messages
        if _is_operator(m)
    )


# Tipos de media REAL (comprobante, tutorial en video, audio, doc). Se excluyen a
# proposito 'chat'/'missed'/'template'/'location', que NO son un adjunto del operador
# (un texto guardado como 'chat' no debe contar como "mando el comprobante/tutorial").
MEDIA_TYPES = frozenset({"image", "video", "audio", "voice", "ptt", "document",
                         "application", "sticker", "viewonce"})


def is_real_media(media_type: str | None) -> bool:
    """El `media_type` de un mensaje es un ADJUNTO de verdad?

    FUENTE UNICA de la respuesta: la usan `operator_sent_media` (aca) y `es_pedido`
    (src/agilidad.py). Antes cada modulo decidia por su cuenta y agilidad lo hacia con
    un chequeo de truthiness, que daba True para 'chat' — el media_type de CUALQUIER
    texto de WhatsApp (679.081 filas en la copia) — y dejaba su regla de cortesia como
    codigo muerto.

    Tolera la forma MIME ('image/jpeg') ademas del token pelado ('image') que guarda
    esta BD: se queda con el tipo principal antes de la barra.
    """
    principal = (media_type or "").strip().lower().split("/")[0]
    return principal in MEDIA_TYPES


def operator_sent_media(messages: list[dict]) -> bool:
    """True si el OPERADOR mando MEDIA real (comprobante de retiro, video-tutorial, etc.).

    El modelo no puede leer la media; asumir fracaso por eso es el error #3 de la
    auditoria. Si el operador la mando, es evidencia de que atendio.
    """
    return any(
        _is_operator(m) and is_real_media(m.get("media_type"))
        for m in messages
    )


# ¿El cliente planteó una CONSULTA contestable? (signo de pregunta o palabra interrogativa).
# Si NO, en 'info' no hay nada que "no responder": el piso se cumple respondiendo cordial
# (trampa de abandono/sin-necesidad). Evita el falso deficiente del saludo/gracias/abandono,
# SIN pisar el caso legítimo donde el cliente sí preguntó algo y el operador lo evadió.
# Se normalizan acentos (á->a) para no fallar por acentos compuestos/descompuestos o faltantes.
_Q_WORDS_RE = re.compile(
    r"\b(como|cuand|cuant|donde|que|cual|por que|se puede|puedo|"
    r"necesito saber|quiero saber|me explic|no se como)\b"
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def client_asked_question(messages: list[dict]) -> bool:
    """True si algún mensaje del CLIENTE contiene una consulta contestable."""
    for m in messages:
        if m.get("from_me") or m.get("is_note"):
            continue
        body = m.get("body") or ""
        if "?" in body or "¿" in body or _Q_WORDS_RE.search(_strip_accents(body)):
            return True
    return False


# Pings de DESESPERACION del cliente (re-pregunta/insistencia sin respuesta): solo
# signos de pregunta ("?"/"??"), o palabras de reclamo por silencio. Se normalizan
# acentos. Son la marca de que el cliente tuvo que insistir, no de una consulta normal.
_REASK_PING_RE = re.compile(
    r"\b(ayuda|auxilio|me responden?|respond[ae](me|n)?|alguien( ahi)?|"
    r"sigue[sn]? ahi|est[a]?[sn]? ahi|hol[ao]+\?)\b"
)


def _is_reask_ping(body: str) -> bool:
    """True si el mensaje del cliente es un ping de insistencia (solo '?' o reclamo de silencio)."""
    b = _strip_accents((body or "").strip().lower())
    if not b:
        return False
    if re.fullmatch(r"[?¿]+", b):
        return True
    return bool(_REASK_PING_RE.search(b))


# Cuanto silencio del negocio hace falta para poder decir que el cliente fue IGNORADO.
# MEDIDO el 2026-08-07 sobre 3.000 sesiones: sin esta condicion, el 50,6% de la rama por
# conteo eran 4+ mensajes del cliente en MENOS DE UN MINUTO (79,1% en la rama del ping), y
# solo 74 de 449 disparos (16,5%) superaban los 5 minutos. El negocio sospechaba de la
# duplicacion de mensajes, pero eso explicaba apenas el 2,2%: el confusor grande eran las
# RAFAGAS (23,2%), o sea como escribe la gente — un run incluia "Listo eso era tdo gracias".
# `friccion` SIEMPRE demota a 'deficiente', asi que cada falso positivo eran 2 estrellas
# injustas para quien atendio a alguien que escribe rapido.
MIN_SILENCIO_FRICCION = timedelta(minutes=5)


def client_reasked(messages: list[dict], *, min_run: int = 4,
                   min_silencio: timedelta = MIN_SILENCIO_FRICCION) -> bool:
    """True si hubo FRICCION: el cliente reinsistio y el negocio lo dejo callado.

    Senal determinista de que el cliente quedo colgado (agnostica al motivo). Exige LAS
    DOS cosas:
      1. una corrida de mensajes CONSECUTIVOS del cliente sin respuesta del negocio, que
         llegue a `min_run` o que tenga >=2 con un ping de desesperacion ("?", "ayuda",
         "me responden");
      2. que dentro de esa corrida el negocio haya estado callado al menos
         `min_silencio` — o sea que TUVO TIEMPO de responder y no lo hizo.
    El reloj arranca en el ultimo mensaje del negocio (o en el primero del cliente si el
    negocio todavia no hablo): es el silencio real que sufrio el cliente.

    El caso multi-transaccion (el cliente manda mucho pero el operador contesta entre
    medio) NO dispara, porque cada respuesta del negocio corta la corrida.

    SIN `created_at` no dispara. No se puede afirmar que hubo silencio sin relojes, y
    demotar a 'deficiente' sin evidencia es peor que perder la señal. En produccion el
    worker scorea por SESION y ahi los timestamps siempre vienen (ver
    src/context.fetch_session_messages); el path por-conversacion de scripts/ no los trae.
    """
    run = 0
    run_has_ping = False
    inicio_silencio = None   # ultimo mensaje del negocio, o 1ro del cliente de la corrida
    for m in messages:
        if m.get("is_note"):
            continue
        if m.get("from_me"):  # el negocio respondio -> corta la corrida
            run = 0
            run_has_ping = False
            inicio_silencio = m.get("created_at")
            continue
        run += 1  # mensaje del cliente
        if inicio_silencio is None:
            inicio_silencio = m.get("created_at")
        if _is_reask_ping(m.get("body") or ""):
            run_has_ping = True
        if not (run >= min_run or (run >= 2 and run_has_ping)):
            continue
        ahora = m.get("created_at")
        if ahora is None or inicio_silencio is None:
            continue  # sin relojes no se puede probar el silencio
        if ahora - inicio_silencio >= min_silencio:
            return True
    return False


def operator_resolved(messages: list[dict]) -> bool:
    """El operador atendio el motivo de forma determinista: confirmo o mando media.

    Senal combinada que usan el scorer (piso) y el router (no skipear un deposito
    estandar donde el cliente solo mando el comprobante).
    """
    return operator_confirmation(messages) or operator_sent_media(messages)


# Empuje comercial del operador (eje 'atencion'=empujo): manda un LINK (registro/
# recarga), invita explicitamente, o presenta un bono ATADO a una recarga. La
# auditoria mostro que el modelo marca 'pasivo' aunque el operador claramente empuja.
PUSH_PATTERN = (
    r"https?://|t[ei] invit|aprovech|no te pierdas|reg[íi]strate|"
    r"obten[eé]s un bono|obtienes un bono|por tu (primera|segunda|pr[oó]xima) recarga|"
    # ofrecer/guiar el alta y presentar promos cuenta como EMPUJO (aunque el motivo no sea
    # promo): 'te creo un usuario', 'te registro', menciones de bono/promo/freebet/giros.
    r"te creo (un |tu )?(usuario|cuenta)|creo tu (usuario|cuenta)|te (ayudo a )?registr|"
    r"te registro|\bbono\b|\bpromo|freebet|giros (gratis|de regalo)"
)
_PUSH_RE = re.compile(PUSH_PATTERN, re.IGNORECASE)

# Maltrato GRAVE del operador (unico gatillo legitimo de 'mala'=1 estrella). Patron
# DELIBERADAMENTE conservador y de alta precision: insultos/agresion explicitos.
# Casi nunca dispara (el maltrato del operador es rarisimo), asi que 'mala' queda
# reservado a evidencia real y todo lo demas cae a 'deficiente' (ver scorer).
MALTRATO_PATTERN = (
    r"\b(idiota|est[uú]pid\w*|imb[eé]cil|c[aá]llate|no me molest\w*|no jodas|"
    r"grosero|malcriado|no seas \w+|dej[aá] de fregar|l[aá]rgate|no me interesa tu)\b"
)
_MALTRATO_RE = re.compile(MALTRATO_PATTERN, re.IGNORECASE)


def operator_pushed(messages: list[dict]) -> bool:
    """True si el OPERADOR empujo conversion/retencion (link, invitacion, bono por recarga).

    Señal AMPLIA: sirve para el PISO del front-of-funnel (explicar la promo YA cuenta) y
    para el eje atencion. NO se usa para el UPLIFT: habia un `operator_strong_uplift` que
    exigia una accion concreta para licenciar buena/excelente, y se retiro el 2026-08-11
    junto con el cap de `promo` al que alimentaba — medido, apuntaba al reves (ver la nota
    de tests/test_scorer.py). El uplift de promo lo decide el MATERIAL, en src/promo.py.
    """
    return any(_PUSH_RE.search(m.get("body") or "") for m in messages if _is_operator(m))


def operator_maltrato(messages: list[dict]) -> bool:
    """True si hay maltrato GRAVE del operador (insulto/agresion explicita)."""
    return any(_MALTRATO_RE.search(m.get("body") or "") for m in messages if _is_operator(m))


# Credenciales de alta manual: la cuenta se creo desde el operador y el OPERADOR le
# entrega usuario/contrasena al cliente. Es evidencia determinista de alto valor
# para el coaching (Capa 1 de recomendaciones): sugiere pedirle al cliente que
# cambie la contrasena en su primer ingreso.
# ENTREGA de credenciales: exige que despues de la etiqueta venga un VALOR en la
# MISMA linea ([ \t] no cruza saltos), o una frase inequivoca de entrega ("tu
# usuario es X", "tus credenciales de acceso"). Asi NO dispara cuando el operador
# PIDE los datos ("envíame tu usuario", un formulario "usuario: ____").
# Patrones SIN acentos (se comparan sobre el texto normalizado con _strip_accents,
# que ademas pasa a minusculas). Asi "contrasena" cubre "contraseña" y los verbos
# de pedido con tilde en la raiz ("indicame", "pasame") matchean igual.
CREDENTIALS_PATTERN = (
    r"(usuario|user)\s*[:=][ \t]*\S+|"
    r"(clave|contrasena|pass)\s*[:=][ \t]*\S+|"
    r"tu (usuario|clave|contrasena) es\b|"
    r"(tus|las) credenciales|credenciales (de acceso|de tu cuenta|son)"
)
_CREDENTIALS_RE = re.compile(CREDENTIALS_PATTERN)

# Verbos de PEDIDO: si el operador ESTA pidiendo los datos (no entregandolos), no
# cuenta como entrega aunque haya una etiqueta con dos puntos y un valor.
ASK_CREDENTIALS_PATTERN = (
    r"envia|enviame|pasame|pasa |mandame|manda |indica|indicame|"
    r"cual es|necesito|dame|proporcion|comparte|apunta"
)
_ASK_CREDENTIALS_RE = re.compile(ASK_CREDENTIALS_PATTERN)


def operator_sent_credentials(messages: list[dict]) -> bool:
    """True si el OPERADOR ENTREGO credenciales (usuario/contrasena con su valor) de
    una cuenta creada por el operador. Distingue entregar de PEDIR: un mensaje que
    pide los datos (verbo de pedido) no cuenta aunque tenga una etiqueta con valor."""
    for m in messages:
        if not _is_operator(m):
            continue
        body = _strip_accents(m.get("body") or "")
        if _CREDENTIALS_RE.search(body) and not _ASK_CREDENTIALS_RE.search(body):
            return True
    return False


# Mencion de "app"/"aplicacion" por CUALQUIERA (cliente o operador): el negocio no
# tiene app disponible, asi que esta senal sirve para recomendar guiar al cliente
# a la web en vez de prometer o buscar una app inexistente.
APP_MENTIONED_PATTERN = r"\bapp\b|aplicaci[oó]n|desc[aá]rga\w*\s+la\s+app"
_APP_MENTIONED_RE = re.compile(APP_MENTIONED_PATTERN, re.IGNORECASE)


def app_mentioned(messages: list[dict]) -> bool:
    """True si algun mensaje (cliente o operador), sin contar notas, menciona la app."""
    return any(
        _APP_MENTIONED_RE.search(m.get("body") or "")
        for m in messages
        if not m.get("is_note")
    )


# --- ABANDONO DEL CLIENTE TRAS UN PEDIDO DEL OPERADOR ---------------------------
# El HECHO que le faltaba al modelo. Medido el 2026-08-07: el 92,9% de las sesiones de
# `sistemas` y el 95,3% de `datos` terminan hablando el operador, asi que "el cliente no
# contesto" NO informa nada — es la forma normal de cerrar (el top-18 de ultimos mensajes
# explica el 86,9% y son plantillas de despedida, mas el token `ing` de acreditacion).
#
# Lo que SI informa es la combinacion: el operador PIDIO u OFRECIO algo concreto y el
# cliente no volvio. Ahi el tramite quedo abierto por el CLIENTE, no por el operador, y
# reprocharle "no completo el registro" es calificarlo por algo que no controla. Medido
# sobre el baseline de 130k, las sesiones con marcador de abandono promediaban 2,82 contra
# 3,09, con 40,3% en 1-2 estrellas contra 31,1%.
#
# Se le pasa al modelo como HINT (no se usa para mover la nota por codigo): el modelo tiene
# que poder decir "hizo lo que podia, y lo mejorable es X". Determinismo para el hecho
# verificable, juicio para el modelo.
_PEDIDO_PENDIENTE_PATTERN = (
    # ofrecer hacer el alta (el empuje concreto de este negocio, no un link)
    r"te creo (un |tu )?(usuario|cuenta)|creo tu (usuario|cuenta)|"
    r"te (ayudo a )?registr|te registro|te abro (la|tu) cuenta|"
    # pedir datos o una accion al cliente
    # OJO con la forma de `env[ií]a`: va el IMPERATIVO dirigido al cliente ("envia tus
    # datos", "enviar el comprobante"), NO la promesa del operador en primera persona
    # ("te ENVIAREMOS el comprobante"), que es la plantilla con la que se CONFIRMA un
    # retiro. Un `\w*` suelto se comia las dos y no distinguia "te pedi algo" de "yo te
    # prometo algo". MEDIDO el 2026-08-12 sobre el rescore v5: era el 99,0% de los
    # abandonos de `retiro` (101 de 102) y el 90,7% de los de agilidad en 5 estrellas
    # (342 de 377). El lookahead corta futuro (enviare/enviaremos), presente en primera
    # persona plural (enviamos) y condicional (enviaria/enviariamos).
    r"pasame|pásame|mandame|mándame|env[ií]ame|"
    r"env[ií]a(?!r[eé]|mos\b|r[ií]a)\w* (tus|los|el)|"
    r"indicame|indícame|compartime|dame (tu|los|el)|"
    r"necesito (tus|los|el|que)|nos falta|confirmame|confírmame|adjunta|"
    # proponer y quedar esperando el si
    r"quieres que|queres que|deseas que|te parece si|te gustar[ií]a|"
    # LA PLANTILLA DE PROSPECCION, la de mayor volumen del negocio: "Con gusto te ayudo con
    # tu registro. Animate y me avisas para crear tu cuenta". El patron pedia `me avisas si`
    # -- con "si" -- y la plantilla dice "me avisas PARA": nunca matcheaba. MEDIDO el
    # 2026-08-12 sobre la copia: de 252 sesiones habia 85 candidatos a abandono y 10
    # marcados; **25 de los que se escapaban eran por esta forma de pedir**. Y es el pedido
    # con mas intencion del embudo: el cliente pregunto por la promo, le ofrecieron crear la
    # cuenta y se fue.
    r"me avisas|me avis[aá]s|an[ií]mate|te ayudo con tu registro|te creo el usuario"
)
_PEDIDO_PENDIENTE_RE = re.compile(_PEDIDO_PENDIENTE_PATTERN, re.IGNORECASE)

# DECIR NO **NO** ES ABANDONAR. Hallado leyendo los 10 abandonos de produccion el 2026-08-12:
# `5011a22b` tenia al cliente diciendo "No gracias publicidad engañosa hacen" y quedaba
# marcado como abandono, porque el operador siguio empujando DESPUES del rechazo y ese empuje
# caia en el tramo final. Pero el cliente ya habia contestado, y contesto que no.
# Son desenlaces distintos para el negocio: el silencio es una FUGA del embudo (arreglable,
# simplificando el pedido de datos); el rechazo es un lead perdido y no hay nada que corregir.
# MEDIDO: 525 conversaciones de la copia tienen un rechazo explicito del cliente.
_RECHAZO_CLIENTE_RE = re.compile(
    r"\b(no gracias|no me interesa|no quiero|ya no quiero|no deseo|"
    r"otro d[ií]a|m[aá]s adelante|"
    r"(es|son) una? (estafa|fraude|robo)|publicidad enga[nñ]osa|enga[nñ]an?)\b",
    re.IGNORECASE)


# `ack` = estado de entrega de WhatsApp, y viene en `messages` al 100% (3.303.952 filas):
#   <0 fallo · 0 pendiente · 1 enviado · 2 entregado · 3 LEIDO · 4 escuchado
_ACK_LEIDO = 3
_ACK_ENTREGADO = 2


def _cliente_lo_leyo(m: dict) -> bool:
    """El cliente LEYO este mensaje del operador (objeto, no inferencia).

    `ack` ausente -> True: los transcripts que no lo traen (el path por conversacion, o un
    fixture armado a mano) no deben PERDER la señal por una columna que no vino; se degrada
    al comportamiento anterior. El contrato de que la consulta real lo trae lo fija
    tests/test_context.py::test_fetch_session_messages_devuelve_ack.
    """
    ack = m.get("ack")
    return True if ack is None else ack >= _ACK_LEIDO


def cliente_abandono_tras_pedido(messages: list[dict]) -> bool:
    """El operador pidio/ofrecio algo concreto, el cliente LO LEYO y NO volvio a escribir.

    Se mira solo el TRAMO FINAL: los mensajes del negocio posteriores al ultimo mensaje
    del cliente. Un pedido que el cliente SI contesto no quedo pendiente, aunque despues
    la sesion cierre con el operador hablando.

    No cuenta como pedido pendiente:
      - la formula de cierre "¿algo mas?" (es cortesia, no un tramite abierto)
      - una confirmacion de transaccion ('ing', 'acreditado'): cierra, no pide
      - sin mensajes del cliente (eso es prospeccion saliente: `no_customer_reply`)
      - **un pedido que el cliente NUNCA LEYO** (ver abajo)

    EL PEDIDO TIENE QUE HABER LLEGADO A LA VISTA DEL CLIENTE. Esta señal existe para NO
    castigar al operador por alguien que se fue, y en `score_by_motivo` levanta el techo de
    `registro`. Pero "irse" es una DECISION del cliente, y no hay decision si nunca vio el
    pedido: ahi el mensaje del operador quedo sin validar, y lo conservador es que el techo
    SI aplique en vez de habilitar la nota maxima.
    Medido el 2026-08-11 sobre las 560 sesiones donde el techo se escapaba: 232 (41,4%) el
    cliente LO LEYO y se fue, 284 (50,7%) le llego y no lo abrio nunca, 44 (7,9%) ni se
    entrego. La logica vieja les daba la misma nota a las tres (4,04 contra 4,20): no
    distinguia al operador que perdio a un cliente presente del que le escribio al vacio.
    """
    return desenlace_del_cliente(messages) == "se_fue"


def _es_pedido(body: str) -> bool:
    """El mensaje del operador PIDE u OFRECE algo concreto (no es cortesia ni confirmacion)."""
    if not body or _ANYTHING_ELSE_RE.search(body):
        return False
    if _PEDIDO_PENDIENTE_RE.search(body):
        return True
    return "?" in body and not _CONFIRMATION_RE.search(body)


def desenlace_del_cliente(messages: list[dict]) -> str | None:
    """QUE PASO CON EL CLIENTE cuando quedo un pedido del operador sin responder.

    `cliente_abandono_tras_pedido` responde "¿le perdonamos al operador?" y para eso exige que
    el cliente haya LEIDO el pedido. Esta responde la otra pregunta -- "¿que paso con el
    cliente?" --, que es la que necesita el NEGOCIO y hoy no se veia en ninguna parte.
    MEDIDO el 2026-08-12 sobre la copia, 252 sesiones evaluadas: 48 clientes recibieron el
    pedido y NUNCA LO ABRIERON, y 11 no lo recibieron. Esos 59 desenlaces eran invisibles.

    Cuatro finales, mutuamente excluyentes y accionables de forma DISTINTA:
        se_fue       lo leyo y no volvio  -> fuga del embudo: el pedido no lo convencio
        no_lo_abrio  le llego, no lo vio  -> lead frio: el canal no lo alcanza
        no_le_llego  no se entrego        -> problema tecnico o numero muerto
        dijo_no      contesto que no      -> lead perdido, no hay nada que corregir
    None = no quedo nada pendiente (el cliente contesto, o el operador no pidio nada).

    ES LA FUENTE UNICA: el booleano de arriba se deriva de aca, para que la regla no viva en
    dos lugares y pueda divergir (la leccion de `_OPERADOR_RESUELTO`, que estaba inline en 5
    consultas distintas).
    """
    reales = [m for m in messages if not m.get("is_note")]
    if not any(m.get("from_me") for m in reales):
        return None
    idx_cliente = [i for i, m in enumerate(reales) if not m.get("from_me")]
    if not idx_cliente:
        return None
    ultimo_del_cliente = reales[idx_cliente[-1]]
    tramo = reales[idx_cliente[-1] + 1:]
    pedidos = [m for m in tramo if _es_pedido((m.get("body") or "").strip())]
    if not pedidos:
        return None
    # Si lo ULTIMO que dijo el cliente fue un NO, no se fue en silencio: contesto. Lo que el
    # operador siga empujando despues no lo convierte en abandono.
    if _RECHAZO_CLIENTE_RE.search(ultimo_del_cliente.get("body") or ""):
        return "dijo_no"
    if any(_cliente_lo_leyo(m) for m in pedidos):
        return "se_fue"
    # No lo leyo: distinguir "le llego" de "no se entrego" por el mejor ack de los pedidos.
    mejor = max((m.get("ack") for m in pedidos if m.get("ack") is not None), default=None)
    return "no_lo_abrio" if mejor == _ACK_ENTREGADO else "no_le_llego"


# --- LA PREGUNTA DE CIERRE, Y LA ESPERA QUE LA HACE VALER ------------------------
# El mensaje canonico del negocio es "¿Hay algo más en lo que te pueda ayudar? 🙂🍀", y la
# regla es ponerlo Y ESPERAR la respuesta. MEDIDO el 2026-08-07 sobre 2.493 sesiones: 280
# mandaron la pregunta y **193 (69 por ciento) cerraron el ticket en menos de un minuto**.
# Mediana de espera antes de cerrar: **0,0 minutos**; p75 = 0,1. Solo 29 pasaron los 5 min.
# O sea que la pregunta se escribe y el ticket se cierra en el mismo instante: el cliente no
# tiene ventana para una segunda duda, y cuatro rubricas deterministas (deposito, retiro,
# info, soporte) daban credito de uplift SOLO por haberla escrito.
# Regla del negocio: el minimo son 5 MINUTOS.
MIN_ESPERA_CIERRE = timedelta(minutes=5)


def operator_asked_and_waited(messages: list[dict], cierre_at=None,
                              min_espera: timedelta = MIN_ESPERA_CIERRE) -> bool:
    """Mando la pregunta de cierre Y dejo una ventana real antes de cerrar el ticket.

    Dos formas de cumplir, porque las dos prueban que la ventana existio:
      - el CLIENTE contesto despues de la pregunta (68 de las 280 medidas), o
      - paso al menos `min_espera` entre la pregunta y el cierre del ticket.

    Sin la hora de cierre, o sin reloj en la pregunta, devuelve True: no se puede PROBAR que
    no espero, y quitar credito sin evidencia es el error que ya cometimos con la friccion.
    Lo que se mide es lo que el operador CONTROLA — cuando cierra —, no si el cliente
    contesto.
    """
    reales = [m for m in messages if not m.get("is_note")]
    idx = None
    for i, m in enumerate(reales):
        # _strip_accents como en operator_asked_anything_else: el patron esta sin tildes y el
        # mensaje canonico del negocio lleva "algo MÁS". Sin normalizar no matchea nada — y
        # los tests que esperaban False pasaban igual, por la razon equivocada.
        if _is_operator(m) and _ANYTHING_ELSE_RE.search(_strip_accents(m.get("body") or "")):
            idx = i
    if idx is None:
        return False
    if any(not m.get("from_me") for m in reales[idx + 1:]):
        return True
    pregunta_at = reales[idx].get("created_at")
    if cierre_at is None or pregunta_at is None:
        return True
    return (cierre_at - pregunta_at) >= min_espera
