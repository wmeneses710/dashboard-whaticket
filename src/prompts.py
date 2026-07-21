"""Armado del prompt del scorer y del esquema de salida estructurada.

El LLM recibe:
  - el HILO del ticket (visitas previas) como CONTEXTO, para no juzgar a ciegas
    un fragmento (p. ej. un "gracias" que cierra una visita anterior),
  - la CONVERSACION OBJETIVO (transcript sin notas internas), que es la unica
    que califica.

Emite: dimensions (una nota por dimension + errores[]), rating_label (una
etiqueta permitida) y rating_rationale (el porque, especifico de esa
conversacion). NO emite stars: la estrella se calcula aparte en
src.rubrics.label_to_stars.
"""
from __future__ import annotations

from src.rubrics import MOTIVO_LABELS, MOTIVOS, RubricSpec, get_rubric

# Rotulo del lado "negocio" (from_me=True) segun quien atiende esa rubrica.
_BUSINESS_LABEL = {"human": "Agente", "bot": "Bot"}

# Etiquetas de ATENCION del operador (pasividad portada al pase unificado). ASCII,
# igual que src/passivity.py (no se importa de alli para no crear un ciclo: passivity
# ya importa de este modulo). empujo = impulso concreto de la conversion; pasivo =
# solo saludo/pregunto/informo sin impulsar; no_respondio = casi no atendio.
ATENCION_LABELS = ("empujo", "pasivo", "no_respondio")

# Truncado de transcripts largos para no reventar num_ctx: si hay mas de
# TRANSCRIPT_MAX mensajes reales, se conservan la cabeza (el motivo) y la cola
# (el cierre) con una marca de lo omitido en el medio. El guardarrail duro para
# conversaciones patologicas vive en src/router.py (anomalous_size).
TRANSCRIPT_MAX = 60
TRANSCRIPT_HEAD = 15
TRANSCRIPT_TAIL = 40

_SYSTEM_TEMPLATE = """\
Sos un evaluador de calidad de atencion al cliente de WhatiCket (chats de \
WhatsApp/Facebook/Instagram, espanol rioplatense). Evaluas UNA conversacion \
—una visita del cliente— y emitis una calificacion CUALITATIVA segun la rubrica \
indicada. No inventas numeros: elegis UNA etiqueta del conjunto permitido y la \
justificas con evidencia concreta de los mensajes.

Reglas:
- Evaluas SOLO la CONVERSACION OBJETIVO. Las visitas previas del ticket son \
CONTEXTO para entender continuidad; no las califiques.
- Ignora las notas internas; el cliente no las ve (ya vienen excluidas del texto).
- Juzga la resolucion A NIVEL DE ESTA VISITA: se atendio el motivo y se lo hizo \
avanzar? No penalices porque el caso completo del ticket siga abierto.
- Dar un PASO ACCIONABLE concreto (mandar el formulario/link, pedir los datos, \
indicar el proceso, ofrecer crear la cuenta) CUENTA como hacer avanzar el motivo, \
aunque el caso no cierre en esta visita.
- RESPUESTA IMPLICITA: la respuesta al motivo puede estar CONTENIDA en lo que dijo \
el agente aunque no sea punto-por-punto ni repita la pregunta. Si el agente EXPLICO \
lo que el cliente pidio (p. ej. dijo el proceso: "registrate, verifica y con tu \
primer deposito se activa"), el motivo SE ATENDIO, aunque la info venga dentro de un \
mensaje promocional o de plantilla. NO marques "no explico" / "no respondio" si la \
informacion PEDIDA esta presente en los mensajes del agente; leela y reconocela.
- ABANDONO DEL CLIENTE: si el agente dio una respuesta accionable y el cliente NO \
respondio o se fue, la falta de cierre es del lado del cliente, NO una falla del \
agente. No lo bajes de nota por el silencio del cliente.
- MEDIA ILEGIBLE: los mensajes marcados "[media/sin texto]" son imagenes/audios que \
NO podes ver. No infieras que "no hubo interaccion" ni que el agente fallo por no \
poder leerlas: evalua SOLO el texto legible; si no hay texto suficiente del cliente, \
NO inventes un fracaso.
- TONO: cordial pero informal o con plantilla NO es "cortante". Cortante = seco, sin \
saludo ni cortesia. Juzga el tono por lo escrito, no por su brevedad.
- Si la conversacion es un fragmento (p. ej. el cliente solo agradece), \
interpretalo a la luz del contexto del ticket.
- Regla de techo: si la dimension dominante ({dominante}) falla, la etiqueta no \
puede superar "{techo}".
- Regla de piso: un error grave (info equivocada con dano, o maltrato) fuerza la \
etiqueta a {piso}.{dos_capas}
- El rating_rationale debe ser ESPECIFICO de esta conversacion (que paso, quien, \
por que). Prohibido generico o de plantilla.
- No inventes emociones, quejas, urgencias ni contexto: evalua SOLO lo que esta \
EXPLICITO en los mensajes. Si el cliente no expreso frustracion o apuro, no lo \
asumas. Atribui cada mensaje a quien lo dijo (Cliente vs Agente/Bot); no confundas \
un mensaje del cliente con una accion del agente.

RUBRICA: {rubric}
Dimensiones y criterios:
{criterios}

Etiquetas permitidas (de mejor a peor): {etiquetas}
Cada dimension DEBE llevar una nota concreta de 1 frase citando evidencia del \
chat; no dejes ninguna nota vacia. Devolve tambien la lista de errores concretos \
(vacia si no hay), la etiqueta elegida y su justificacion.

ATENCION DEL OPERADOR (campo "atencion"): ademas de la calificacion, clasifica en \
UNA etiqueta el ESFUERZO del OPERADOR HUMANO (Agente) por impulsar la conversion \
(registro/deposito/apuesta). Juzga SOLO al operador humano: NO al bot, NO al cliente, \
y NO juzgues si el cliente termino depositando (eso es otro eje).
- empujo: el operador IMPULSO CONCRETAMENTE la conversion con una accion real: \
ofrecer/guiar el registro, pedir datos para crear la cuenta, invitar a \
depositar/recargar/apostar, mandar un link, o presentar la promo/bono. Si no hay \
NINGUNA de esas acciones, NO es empujo.
- pasivo: el operador solo saludo, hizo una pregunta suelta, informo o respondio una \
duda SIN impulsar la conversion. Un simple "Hola", "en que te ayudo" o una pregunta \
trivial = pasivo (no ofrecio nada).
- no_respondio: el operador practicamente no atendio lo que el cliente necesitaba.
Ejemplos: "Hola" -> pasivo; "te ayudo a crear tu cuenta?" -> empujo; "en que le puedo \
ayudar?" -> pasivo; "registrate y hace tu primera recarga de $5" -> empujo.

OBSERVACION DE DEPOSITO (campo "deposit_observed"): marca true SOLO si en el \
transcript aparece un comprobante o recarga reconocida (una captura/imagen de pago o \
un mensaje que confirme la recarga); en caso contrario false. Es una OBSERVACION, NO \
una decision: el conteo real de depositos lo dictamina un gate DETERMINISTA aparte y \
ese manda; vos solo reportas lo que se ve en el texto.\
"""

_USER_TEMPLATE = """\
### Contexto del ticket (visitas previas, orden cronologico)
{contexto}

### CONVERSACION OBJETIVO (la unica a calificar)
{transcript}\
"""


def format_transcript(messages: list[dict], rubric: str) -> str:
    """Convierte los mensajes en un transcript legible, excluyendo notas internas.

    `from_me=True` = lado negocio (Agente o Bot segun la rubrica); False = Cliente.
    Los mensajes sin texto (solo media) se marcan para que el LLM lo sepa.
    """
    # Las rubricas de MOTIVO (deposito/retiro/...) no estan en _BUSINESS_LABEL: el
    # lado negocio se rotula 'Agente' (el motivo evalua al operador humano).
    biz = _BUSINESS_LABEL.get(get_rubric(rubric).name, "Agente")
    lines: list[str] = []
    for m in messages:
        if m.get("is_note"):
            continue
        body = (m.get("body") or "").strip() or "[media/sin texto]"
        who = biz if m.get("from_me") else "Cliente"
        lines.append(f"{who}: {body}")
    if len(lines) > TRANSCRIPT_MAX:
        omitidos = len(lines) - TRANSCRIPT_HEAD - TRANSCRIPT_TAIL
        lines = [
            *lines[:TRANSCRIPT_HEAD],
            f"[... {omitidos} mensajes omitidos ...]",
            *lines[-TRANSCRIPT_TAIL:],
        ]
    return "\n".join(lines)


def _dos_capas_block(spec: RubricSpec) -> str:
    """Reglas de las DOS CAPAS (solo rubricas de motivo, con `uplift`). El PISO
    (dimension dominante = resolucion) da 'aceptable' si atendio el motivo aunque sea
    templateado; el UPLIFT (dimension `uplift` + cortesia) permite superarlo. Las
    rubricas legacy (human/bot, sin uplift) NO llevan estas reglas."""
    if not spec.uplift:
        return ""
    upl = next(d for d in spec.dimensions if d.key == spec.uplift)
    return (
        "\n- MODELO DE DOS CAPAS (calibracion de la etiqueta):\n"
        f"  PISO: si el agente ATENDIO el motivo (dimension {spec.dominant}) de forma "
        'correcta, aunque sea minima o con PLANTILLA, la etiqueta es "aceptable". '
        "La plantilla NO baja la nota (ver regla de tono).\n"
        "  DEBAJO DEL PISO: si NO atendio el motivo (no resolvio, dato erroneo, maltrato, "
        'o cerro muy rapido sin resolver), la etiqueta no supera "deficiente".\n'
        '  UPLIFT: para superar "aceptable" (llegar a "buena"/"excelente") el agente debe '
        f"ADEMAS {upl.bien}, y/o mostrar una cortesia destacada (saludo, personalizacion). "
        'Sin eso, el techo es "aceptable".'
    )


def _criterios_block(spec: RubricSpec) -> str:
    return "\n".join(
        f"- {d.key}: BIEN = {d.bien}. MAL = {d.mal}." for d in spec.dimensions
    )


def _etiquetas_block(spec: RubricSpec) -> str:
    return ", ".join(
        f'"{label}" ({spec.label_to_stars[label]} estrellas)' for label in spec.labels_desc
    )


def _json_shape_block(spec: RubricSpec) -> str:
    """Instruccion con la forma EXACTA del JSON de salida.

    Reemplaza al schema-grammar de Ollama (que rompe con este modelo): pedimos
    el JSON en el prompt y validamos las claves en el scorer.
    """
    dims = ", ".join(f'"{d.key}": "<nota de 1 frase con evidencia>"' for d in spec.dimensions)
    labels = "|".join(spec.labels_desc)
    atencion = "|".join(ATENCION_LABELS)
    return (
        "Responde UNICAMENTE con un objeto JSON valido, sin texto fuera del JSON, "
        "con esta forma EXACTA:\n"
        '{"dimensions": {' + dims + ', "errores": []}, '
        f'"rating_label": "<una de: {labels}>", '
        '"rating_rationale": "<2-4 frases especificas de esta conversacion>", '
        f'"atencion": "<una de: {atencion}>", '
        '"deposit_observed": <true|false>}'
    )


# ============================================================================
# Pase v2: el LLM clasifica el MOTIVO (tabla de motivos) y califica en 2 capas.
# Reemplaza la eleccion de rubrica por handler (human/bot). El determinista quedo
# descartado para el motivo (31% 'otro' + contamina; ver docs/diseno-scoring-v2.md).
# ============================================================================
_MOTIVO_SYSTEM = """\
Sos un evaluador de calidad de atencion al cliente de una plataforma de apuestas \
(chats de WhatsApp/Facebook, espanol rioplatense/ecuatoriano). Evaluas UNA SESION (la \
interaccion de UN agente con el cliente) y emitis: el MOTIVO de la interaccion, una \
calificacion cualitativa y la clasificacion de la atencion del operador.

Reglas generales:
- Evaluas al OPERADOR HUMANO (Agente). El Bot y el Cliente no se califican.
- Ignora las notas internas (ya vienen excluidas del texto).
- RESPUESTA IMPLICITA: la respuesta al motivo puede estar CONTENIDA en lo que dijo el \
agente aunque no repita la pregunta. Si la info pedida esta presente, el motivo SE ATENDIO.
- ABANDONO DEL CLIENTE: si el agente dio una respuesta accionable y el cliente se fue, la \
falta de cierre es del CLIENTE, no una falla del agente.
- MEDIA ILEGIBLE: los "[media/sin texto]" son imagenes/audios que NO podes ver. En \
depositos/retiros el comprobante suele venir como media: NO asumas fracaso por no verla.
- TONO: cordial pero con PLANTILLA NO es cortante. Templateado y correcto es aceptable.
- No inventes emociones ni contexto: evalua SOLO lo EXPLICITO en los mensajes. Atribui \
cada mensaje a quien lo dijo (Cliente vs Agente/Bot).

PASO 1 - MOTIVO. Clasifica la interaccion en UNO de estos motivos (campo "motivo"):
{tabla}

PASO 2 - CALIFICACION (modelo de DOS CAPAS, segun el motivo elegido):
- PISO: si el agente ATENDIO el motivo (columna PISO) correctamente, aunque sea minimo o \
templateado, la etiqueta es "aceptable". La plantilla NO baja la nota.
- DEBAJO DEL PISO: si NO atendio el motivo (no resolvio, dato erroneo, maltrato, o cerro \
muy rapido sin resolver), la etiqueta no supera "deficiente".
- UPLIFT: para superar "aceptable" (llegar a "buena"/"excelente") el agente debe ADEMAS \
hacer la accion extra del motivo (columna UPLIFT) y/o mostrar cortesia destacada.

Dimensiones (una nota de 1 frase con evidencia del chat cada una): resolucion (atendio el \
motivo = PISO), iniciativa (accion extra = UPLIFT), cortesia (saludo, palabras, \
personalizacion). Mas la lista de errores concretos (vacia si no hay).

Etiquetas permitidas (de mejor a peor): {etiquetas}

ATENCION DEL OPERADOR (campo "atencion") - esfuerzo del AGENTE HUMANO por impulsar la \
conversion/retencion (NO al bot, NO al cliente):
- empujo: impulso concreto (ofrecer/guiar registro, invitar a depositar/recargar/apostar, \
mandar link, presentar promo/bono, o retener en un retiro invitando a volver a jugar).
- pasivo: solo saludo, informo o pregunto SIN impulsar.
- no_respondio: casi no atendio lo que el cliente necesitaba.

OBSERVACION DE DEPOSITO (campo "deposit_observed"): true si en el transcript aparece un \
comprobante/recarga reconocida; false si no. Es OBSERVACION, no decision: el conteo real \
lo dicta un gate DETERMINISTA aparte.{hint}

{json_shape}"""

_MOTIVO_HINT = (
    "\n\nHINT DETERMINISTA: se detecto un comprobante de deposito/recarga en el "
    'transcript. El motivo muy probablemente sea "deposito" (salvo que el texto indique '
    "claramente otra cosa)."
)

_MOTIVO_JSON_SHAPE = (
    "Responde UNICAMENTE con un objeto JSON valido, sin texto fuera del JSON, con esta "
    "forma EXACTA:\n"
    '{"motivo": "<uno de: ' + "|".join(MOTIVOS) + '">, '
    '"dimensions": {"resolucion": "<nota 1 frase>", "iniciativa": "<nota 1 frase>", '
    '"cortesia": "<nota 1 frase>", "errores": []}, '
    '"rating_label": "<una de: ' + "|".join(MOTIVO_LABELS) + '">, '
    '"rating_rationale": "<2-4 frases especificas de esta sesion>", '
    '"atencion": "<empujo|pasivo|no_respondio>", '
    '"deposit_observed": <true|false>}'
)


def _motivo_tabla_block() -> str:
    """Tabla de motivos para el prompt: 'motivo: PISO = ... UPLIFT = ...' por cada uno."""
    lines = []
    for m in MOTIVOS:
        spec = get_rubric(m)
        res = next(d for d in spec.dimensions if d.key == spec.dominant)
        upl = next(d for d in spec.dimensions if d.key == spec.uplift)
        lines.append(f"- {m}: PISO = {res.bien}. UPLIFT = {upl.bien}.")
    return "\n".join(lines)


def build_motivo_prompt(
    target_messages: list[dict], thread_context: str, *, deposit_hint: bool = False
) -> tuple[str, str]:
    """Prompt v2: el LLM elige el MOTIVO de la tabla y califica en 2 capas. (system, user)."""
    system = _MOTIVO_SYSTEM.format(
        tabla=_motivo_tabla_block(),
        etiquetas=_etiquetas_block(get_rubric(MOTIVOS[0])),
        hint=_MOTIVO_HINT if deposit_hint else "",
        json_shape=_MOTIVO_JSON_SHAPE,
    )
    contexto = (thread_context or "").strip() or "(sin visitas previas)"
    user = _USER_TEMPLATE.format(
        contexto=contexto, transcript=format_transcript(target_messages, MOTIVOS[0])
    )
    return system, user


def build_motivo_schema() -> dict:
    """Esquema de salida del pase v2: motivo + dimensiones uniformes + label unificado."""
    return {
        "type": "object",
        "properties": {
            "motivo": {"type": "string", "enum": list(MOTIVOS)},
            "dimensions": {
                "type": "object",
                "properties": {
                    "resolucion": {"type": "string"},
                    "iniciativa": {"type": "string"},
                    "cortesia": {"type": "string"},
                    "errores": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["resolucion", "iniciativa", "cortesia"],
            },
            "rating_label": {"type": "string", "enum": list(MOTIVO_LABELS)},
            "rating_rationale": {"type": "string"},
            "atencion": {"type": "string", "enum": list(ATENCION_LABELS)},
            "deposit_observed": {"type": "boolean"},
        },
        # atencion/deposit_observed best-effort (no en required), igual que el pase viejo.
        "required": ["motivo", "dimensions", "rating_label", "rating_rationale"],
    }
