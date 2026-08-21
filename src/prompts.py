"""Armado del prompt del scorer y del esquema de salida estructurada.

El LLM recibe:
  - el HILO del ticket (visitas previas) como CONTEXTO, para no juzgar a ciegas
    un fragmento (p. ej. un "gracias" que cierra una visita anterior),
  - la CONVERSACION OBJETIVO (transcript sin notas internas), que es la unica
    que califica.

Emite: dimensions (una nota por dimension + errores[]), los HECHOS booleanos (una
etiqueta permitida) y rating_rationale (el porque, especifico de esa
conversacion). NO emite stars: la estrella se calcula aparte en
src.rubrics.label_to_stars.
"""
from __future__ import annotations

from src.fewshot import formatear_fewshot
from src.interacciones import es_cierre
from src.catalogo_atc import (
    CODIGOS_ERROR,
    CODIGOS_PRACTICA,
    bloque_para_el_prompt,
    bloque_practicas_para_el_prompt,
)
from src.rubrics import (
    MOTIVO_LABELS,
    MOTIVOS,
    MOTIVOS_DEL_LLM,
    RubricSpec,
    get_rubric,
)

# Rotulo del lado "negocio" (from_me=True) segun quien atiende esa rubrica.
_BUSINESS_LABEL = {"human": "Operador", "bot": "Bot"}

# Etiquetas de ATENCION del operador, portadas del pase de pasividad a este pase
# unificado. empujo = impulso concreto de la conversion; pasivo = solo saludo,
# pregunto o informo sin impulsar; no_respondio = casi no atendio.
# (src/passivity.py, que era el pase original, se elimino el 2026-08-06: quedaba
# superseded por este y no lo usaba nadie fuera de su propio test.)
ATENCION_LABELS = ("empujo", "pasivo", "no_respondio")

# Truncado de transcripts largos para no reventar num_ctx: si hay mas de
# TRANSCRIPT_MAX mensajes reales, se conservan la cabeza (el motivo) y la cola
# (el cierre) con una marca de lo omitido en el medio. El guardarrail duro para
# conversaciones patologicas vive en src/router.py (anomalous_size).
TRANSCRIPT_MAX = 60
TRANSCRIPT_HEAD = 15
TRANSCRIPT_TAIL = 40


_USER_TEMPLATE = """\
### Contexto del ticket (visitas previas, orden cronologico)
{contexto}

### CONVERSACION OBJETIVO (la unica a calificar)
{transcript}\
"""


def _delta(seg: float) -> str:
    """El hueco entre dos mensajes, compacto, para el margen del transcript."""
    if seg < 60:
        return f"+{round(seg)} s"
    if seg < 3600:
        return f"+{round(seg / 60)} min"
    if seg < 86400:
        return f"+{seg / 3600:.1f} h".replace(".0 h", " h")
    d = seg / 86400
    return f"+1 dia" if round(d, 1) == 1.0 else f"+{d:.1f} dias"


def format_transcript(messages: list[dict], rubric: str, *, con_tiempos: bool = False) -> str:
    """Convierte los mensajes en un transcript legible, excluyendo notas internas.

    `from_me=True` = lado negocio (Operador o Bot segun la rubrica); False = Cliente.
    Los mensajes sin texto (solo media) se marcan para que el LLM lo sepa.

    `con_tiempos` (EXPERIMENTAL, apagado por defecto) agrega la HORA DE RELOJ del primer
    mensaje, el DELTA entre mensajes y la FRONTERA de cada interaccion. Sin eso el modelo no
    puede saber si contestaron en 20 segundos o en 20 horas, y una sesion de 17 interacciones
    le llega como un chat plano.
    VA APAGADO A PROPOSITO: darle tiempos crudos tiene un riesgo medible. El HORARIO ya esta
    resuelto de forma determinista (`src/horario.espera_efectiva` descuenta la noche; el 26%
    de los deficientes eran clientes que escribian de madrugada), y hay esperas LEGITIMAS que
    el reloj no distingue -- un retiro que depende del banco, un deposito en validacion. Un
    modelo con timestamps y sin ese contexto castigaria esas esperas. Se prende recien si el
    banco de casos (scripts/eval_prompt.py) demuestra que gana precision SIN romperlas.
    """
    # Las rubricas de MOTIVO (deposito/retiro/...) no estan en _BUSINESS_LABEL: el
    # lado negocio se rotula 'Operador' (el motivo evalua al operador humano).
    biz = _BUSINESS_LABEL.get(get_rubric(rubric).name, "Operador")
    lines: list[str] = []
    previo = None
    for m in messages:
        if m.get("is_note"):
            # La nota de CIERRE es la unica que se emite, y como SEPARADOR: es la frontera
            # de la interaccion (ver src/interacciones.py), no un mensaje del operador.
            if con_tiempos and es_cierre(m):
                lines.append("--- el operador CERRO la interacción aquí ---")
            continue
        body = (m.get("body") or "").strip() or "[media/sin texto]"
        who = biz if m.get("from_me") else "Cliente"
        marca = ""
        if con_tiempos and m.get("created_at") is not None:
            at = m["created_at"]
            marca = (f"[{_delta((at - previo).total_seconds())}] " if previo is not None
                     else f"[{at:%d/%m %H:%M}] ")
            previo = at
        lines.append(f"{who}: {marca}{body}")
    if len(lines) > TRANSCRIPT_MAX:
        omitidos = len(lines) - TRANSCRIPT_HEAD - TRANSCRIPT_TAIL
        lines = [
            *lines[:TRANSCRIPT_HEAD],
            f"[... {omitidos} mensajes omitidos ...]",
            *lines[-TRANSCRIPT_TAIL:],
        ]
    return "\n".join(lines)






_MOTIVO_SYSTEM = """\
Sos un evaluador de calidad de atencion al cliente de una plataforma de apuestas \
(chats de WhatsApp/Facebook, espanol rioplatense/ecuatoriano). Evaluas UNA SESION (la \
interaccion de UN operador con el cliente) y emitis: el MOTIVO de la interaccion, una \
calificacion cualitativa y la clasificacion de la atencion del operador.

Reglas generales:
- Evaluas al OPERADOR HUMANO. El Bot y el Cliente no se califican.
- Ignora las notas internas (ya vienen excluidas del texto).
- RESPUESTA IMPLICITA: la respuesta al motivo puede estar CONTENIDA en lo que dijo el \
operador aunque no repita la pregunta. Si la info pedida esta presente, el motivo SE ATENDIO.
- ABANDONO DEL CLIENTE: si el operador dio una respuesta accionable y el cliente se fue, la \
falta de cierre es del CLIENTE, no una falla del operador.
- MEDIA ILEGIBLE: los "[media/sin texto]" son imagenes/audios que NO podes ver. En \
depositos/retiros el comprobante suele venir como media: NO asumas fracaso por no verla.
- TONO: cordial pero con PLANTILLA NO es cortante. Templateado y correcto es aceptable.
- JERGA AFECTUOSA: el trato coloquial (ñaño, naho, pana, panita, mi rey, causa, amigo/amiga) \
NO es maltrato ni falta de respeto: es cercania. NUNCA lo cuentes como error de tono.
- CLIENTE SIN NECESIDAD: si el cliente solo saluda, agradece, dice "ok" o se despide SIN \
plantear una consulta, y el operador respondio cordial, entonces `atendio_el_motivo` es \
TRUE: no habia nada que resolver mas que responder con cortesia. NO lo pongas en false — \
false significa que HABIA un motivo y no se atendio.
- No inventes emociones ni contexto: evalua SOLO lo EXPLICITO en los mensajes. Atribui \
cada mensaje a quien lo dijo (Cliente vs Operador/Bot).

PASO 1 - MOTIVO. Clasifica la interaccion en UNO de estos motivos (campo "motivo"):
{tabla}

Elegi el motivo por la NECESIDAD PRINCIPAL del cliente (lo que vino a resolver), NO por si
se menciona plata, saldo o un comprobante (un comprobante puede aparecer en CUALQUIER motivo).
Guia rapida de desambiguacion:
- pregunta por saldo / comisiones / como-cuando-cuanto / duda -> info
- interes en un bono o promocion -> promo
- manda un comprobante/recarga para que le ACREDITEN saldo (incluye "Abono N a deuda" \
+ comprobante) -> deposito
- datos de agencia + monto a retirar + cuenta bancaria -> retiro
CLAVE deposito vs retiro: si el COMPROBANTE lo manda el CLIENTE (una captura de pago) es
RECARGA/deposito. En un RETIRO el cliente manda DATOS (agencia, monto, cuenta) y el
COMPROBANTE lo manda el OPERADOR. Cliente adjunta comprobante -> deposito, NO retiro.
- contrasena / cambio de cuenta o nombre / verificacion de identidad -> soporte_cuenta
- ACTIVAR o ACCEDER a una cuenta que YA EXISTE ("como activo mi cuenta", "como accedo", \
"como entro") -> soporte_cuenta, NO registro: la cuenta ya esta creada, el cliente necesita \
ayuda para entrar. (Decision del negocio, 2026-08-07.)
- quiere crear una cuenta NUEVA -> registro
CLAVE registro: si en la sesion SE CREO LA CUENTA (el cliente paso sus datos y el operador \
devolvio usuario y clave) el motivo es `registro`, sin importar que haya pasado antes o \
despues — aunque la conversacion arrancara por una promo o terminara en una recarga. El alta \
es el hecho consumado; la promo fue el gancho. (Decision del negocio, 2026-08-07.)
- algo no funciona / no se le acredito / reclamo -> problema
CLAVE deposito vs problema: si el cliente manda el comprobante AHORA para que le acrediten, \
es `deposito`. Si RECLAMA por una recarga YA HECHA que no se le acredito -- habla en PASADO \
("hice una recarga hace 2 horas y no me aparece"), sin adjuntar nada nuevo -- es `problema`. \
Lo que decide es si viene a que le carguen algo o a reclamar que no le cargaron.

PASO 2 - HECHOS. NO elijas una nota: responde estos HECHOS (los 4 primeros true/false; \
claridad es una etiqueta) y el sistema calcula la nota de forma determinista.
- atendio_el_motivo: el operador ATENDIO el motivo (columna PISO), aunque sea minimo o \
templateado. CUENTAN: la respuesta IMPLICITA, la PLANTILLA correcta ("listo"/"ing"/"cargado") \
y la MEDIA del operador (comprobante de retiro, video-tutorial). Si dio una respuesta accionable \
y el cliente se fue, igual ATENDIO (el abandono es del cliente).
  NO CUENTA UNA DESPEDIDA. "Mucha suerte hoy", "esperamos poder atenderte de nuevo", "un \
placer atenderte", "gracias por preferirnos" son CIERRE de la conversacion, no atencion del \
pedido. Si despues del pedido del cliente el operador SOLO mando una despedida -- nada que \
acuse, confirme ni resuelva lo que el cliente pidio --, `atendio_el_motivo` es FALSE.
  LA DIFERENCIA: "listo"/"ing"/"cargado" ACUSAN el pedido (hablan de lo que el cliente pidio); \
"mucha suerte" no dice NADA del pedido. Una plantilla que acusa atiende; una que solo se \
despide, no. Y esto vale aunque el cliente se haya ido despues: el abandono disculpa la falta \
de CIERRE, no la falta de RESPUESTA.
- hizo_accion_extra: ADEMAS hizo la accion extra del motivo (columna UPLIFT).
- cortesia_destacada: cortesia notable (usa el nombre, calidez real, personaliza). La jerga \
afectuosa (ñaño/pana/panita/mi rey) SUMA, no resta.
- hubo_maltrato_grave: hubo INSULTO o AGRESION explicita del operador. La no-respuesta, una \
respuesta floja o la informalidad NO son maltrato.
- claridad: que tan CLARO fue el operador sobre el objetivo. UNA de: "claro" | "confuso" | "dudoso".
  * claro: el cliente pudo ACCIONAR la respuesta sin adivinar ni volver a preguntar; el proximo \
paso o la info pedida esta EXPLICITA; si uso plantilla, la plantilla RESPONDE lo que ESTE cliente pregunto.
  * confuso: respuesta ambigua/contradictoria, info incompleta que obliga a inferir, o una plantilla \
generica que NO encaja con la pregunta puntual (deflexion tipo "crea tu cuenta" ante una consulta concreta).
  * dudoso: si NO estas seguro (no fuerces "claro" ni "confuso" en un caso borderline).
  El TONO/cortesia NO es claridad: un mensaje seco pero claro es claro; uno calido pero confuso NO lo es.
- cliente_reinsistio: true si el cliente tuvo que VOLVER A ESCRIBIR porque no obtuvo \
respuesta. CUENTAN TODAS estas formas, y la lista NO es cerrada:
  * repetir el pedido, igual o dicho de otra manera;
  * insistir o pedir ayuda de nuevo ("me ayudan?", "hola?", "sigo esperando", "ahi?");
  * RECLAMAR la demora o el SILENCIO ("llevo 40 minutos esperando", "nadie me contesta", \
"me estan ignorando?");
  * un "?" o un "ayuda" suelto.
  La CANTIDAD no importa: alcanza UNA sola vez para que sea true.
  false SOLO si el cliente NO volvio a escribir (se fue callado = abandono) o si volvio \
CONFORME (agradece, confirma que le llego, se despide).

Dimensiones (una nota de 1 frase con evidencia del chat cada una): resolucion (el PISO), \
iniciativa (la accion extra = UPLIFT), cortesia. Mas la lista de errores concretos (vacia si no hay).

RECOMENDACION (campo "recomendacion"): UN consejo concreto y accionable para el OPERADOR, \
anclado en las REGLAS DE NEGOCIO de abajo (no un coaching generico). En ESPANOL NEUTRO y \
profesional, SIN voseo ni regionalismos rioplatenses en el texto del consejo. Devuelve "" \
solo si ya fue excelente y no aplica ninguna regla.
REGLA GENERAL: NO recomiendes algo que el operador YA HIZO en el chat (no sugieras mandar el \
enlace si ya mando uno, ni invitar a depositar/registrarse si ya lo hizo); reconoce lo hecho \
y apunta al SIGUIENTE paso real.
REGLAS POR MOTIVO:
- registro: el alta la hace el OPERADOR, dentro del chat: pide los datos (correo, celular, \
nombre de usuario) y devuelve usuario y clave. Ese es el proceso completo. Si el operador creo la cuenta (mando usuario/clave en el chat), recuerda que el \
cliente debe cambiar la contrasena en su primer ingreso. Si se piden datos sensibles \
(cedula/datos), aclara para que son y que estan protegidos. Aclara que el beneficio se activa \
con el primer deposito (puede depender del monto, salvo promo) y se verifica en la pagina.
- deposito: al ofrecer un bono, avisa QUE se gana, CUANDO y COMO se libera (hay que apostar lo \
depositado y el bono solo se usa en apuestas con requisitos), porque si el cliente no lo sabe \
termina reclamando. NO inventes porcentajes de campana que no conoces.
- promo: igual que el bono; explica el requisito de la promo y el camino de reclamo (registrarse \
-> primer deposito -> se activa, o la accion puntual del evento).
- retiro: da un tiempo estimado para dar tranquilidad e invita a escribir si se pasa de ese \
tiempo. La verificacion se exige en retiros por la pagina, no por mensaje.
- info: responde la duda con CLARIDAD y ADELANTATE al hueco encadenado (si explicas como \
apostar, cubre tambien que es una cuota y los tipos de apuesta) para no dejar al cliente a \
medias. Empuja el registro/deposito solo si la duda es de bono/promo/producto, no en una duda \
tecnica de una apuesta ya hecha.
- soporte_cuenta: si se reseteo la contrasena, pide que la cambie al ingresar; no pidas \
documentos sensibles salvo que el cliente quiera retirar o acceder a beneficios.
- problema: reconoce el problema y resuelvelo o escalalo al area correcta con un tiempo \
estimado; no cierres sin resolver ni derives a redes sociales.
REGLA APP: no hay app todavia; si el cliente la pide, guialo a usar la web (la app llega \
proximamente). Nunca lo mandes a descargar una app.
El tono informal/cercano del operador (bro, pana, ñaño) esta PERMITIDO: NO lo marques como algo \
a corregir en la recomendacion.
Ej: "Confirmaste la recarga; la proxima menciona el bono de la segunda recarga y como se libera".

ATENCION DEL OPERADOR (campo "atencion") - esfuerzo del OPERADOR HUMANO por impulsar la \
conversion/retencion (NO al bot, NO al cliente):
- empujo: impulso concreto (ofrecer/guiar registro, invitar a depositar/recargar/apostar, \
mandar link, presentar promo/bono, o retener en un retiro invitando a volver a jugar).
- pasivo: solo saludo, informo o pregunto SIN impulsar.
- no_respondio: casi no atendio lo que el cliente necesitaba.

OBSERVACION DE DEPOSITO (campo "deposit_observed"): true si en el transcript aparece un \
comprobante/recarga reconocida; false si no. Es OBSERVACION, no decision: el conteo real \
lo dicta un gate DETERMINISTA aparte.{hint}

{ejemplos}

{json_shape}"""

_MOTIVO_HINT = (
    "\n\nHINT DETERMINISTA: el CLIENTE adjunto un comprobante de pago. Eso es una RECARGA "
    '(deposito), NO un retiro (en un retiro el comprobante lo manda el operador). El motivo '
    'es "deposito", salvo que el texto del cliente pida claramente otra cosa (consulta, '
    "promo, soporte) y el comprobante sea secundario."
)

_ABANDONO_HINT = (
    "\n\nHINT DETERMINISTA: el operador PIDIO u OFRECIO algo concreto (crear la cuenta, "
    "los datos, una confirmacion) y el CLIENTE NO VOLVIO A ESCRIBIR. El tramite quedo "
    "abierto por el CLIENTE, no por el operador. NO cuentes como error del operador lo que "
    "dependia de esa respuesta que nunca llego (p. ej. 'no creo la cuenta', 'no completo el "
    "registro'): si ofrecio hacerlo, atendio el motivo. Lo mejorable va en la "
    "RECOMENDACION, no en `errores`: por ejemplo si no explico COMO sigue el tramite, si no "
    "aclaro que datos necesita y para que, o si arranco pidiendo datos personales sin "
    "generar confianza primero."
)

# CONTRATO DE CADA CAMPO. Lo agrego el 2026-08-07 despues de medir con el modelo de prod
# sobre 45 sesiones: el 44,4% mostraba la critica DENTRO del panel de aciertos y el 44,4%
# tenia una nota de 4-5 con un rationale que la desmentia. La causa no era el modelo
# portandose mal: le pediamos una evaluacion BALANCEADA por dimension ("hizo X pero no Y") y
# despues el codigo la partia en positivos y negativos, asi que los positivos heredaban el
# reproche. Ademas el 68,9% no producia ningun `errores[]`, o sea que la critica no tenia
# donde ir y se mudaba al campo que el front pinta como logro.
# La salida no es post-procesar la prosa, es pedir la separacion EN ORIGEN.
_CAMPOS_CONTRATO = (
    "\n\nCOMO ESCRIBIR CADA CAMPO (es un contrato, no un estilo):\n"
    "- dimensions.resolucion / .iniciativa / .cortesia: describen SOLO LO QUE EL OPERADOR "
    "HIZO en ese eje. PROHIBIDO usar 'pero', 'aunque', 'sin embargo', 'falto' o 'no "
    "completo/confirmo/guio' en estos tres campos: son la evidencia de lo que se hizo BIEN "
    "y se muestran al operador como sus logros. Si en un eje no hizo nada destacable, "
    "describilo en una frase corta y neutra, sin reprochar.\n"
    # DE PROSA LIBRE A CODIGO DEL CATALOGO. Con texto libre el modelo escribia la MISMA
    # falta de cinco formas: MEDIDO el 2026-08-19, 7.019 errores en 3.680 textos distintos
    # (52% unicos), asi que el negocio no podia contar ni comparar nada. Los doce codigos
    # son la lista CERRADA que ATC ya publica en su manual, con su numeracion; el supervisor
    # los conoce por el numero. Ver src/catalogo_atc.py.
    "- dimensions.errores: una lista de CODIGOS del catalogo de errores criticos de ATC "
    "(los de abajo), NO texto libre. Solo lo que el operador pudo haber hecho distinto Y "
    "dependia de el: si algo quedo sin cerrar porque el CLIENTE no respondio, NO va aca. "
    "Puede quedar vacia, y de hecho lo normal es que lo este: una sesion bien atendida no "
    "necesita errores inventados. NO inventes codigos que no esten en la lista.\n"
    "  ERRORES CRITICOS DE ATC (elegi solo de aca):\n" + bloque_para_el_prompt() + "\n"
    # LA MISMA JUGADA QUE v21 HIZO CON LOS ERRORES, ahora del lado positivo. La recomendacion
    # sigue siendo PROSA -- los B dicen QUE y el operador necesita el COMO (ver
    # tests/test_coaching.py::test_el_coaching_dice_COMO_no_solo_QUE_paso) -- pero declara a
    # QUE practica apunta, y eso la vuelve sumable. MEDIDO: 12.163 recomendaciones del modelo
    # en 10.325 textos distintos (84,9% unicos), o sea hoy no se puede contar ninguna.
    # El catalogo va COMPLETO y con la frase: sin la frase el codigo es una etiqueta que cada
    # corrida interpreta distinto (misma leccion que `bloque_para_el_prompt`).
    "  BUENAS PRACTICAS DE ATC (para `recomendacion_practica`, elegi solo de aca):\n"
    + bloque_practicas_para_el_prompt() + "\n"
    "- recomendacion: aca va TODO lo mejorable, incluido lo que quedo pendiente del cliente "
    "y los matices de venta (ir al punto, explicar como sigue el tramite, generar confianza "
    "antes de pedir datos personales). Es el campo de coaching: usalo.\n"
    # RESTRICCION DE SALIDA, no un hecho para reportar. La version anterior de esta regla
    # vivia en REGLAS POR MOTIVO y decia "EN ESTE NEGOCIO NO EXISTE UN LINK DE REGISTRO";
    # el modelo la leyo como material de coaching y la recito como reproche al operador:
    # "no se menciono que no existe un enlace de registro" (14 de 280 filas de `registro`
    # medidas el 2026-08-07), y hasta cito la regla misma ("lo cual no se permite segun las
    # reglas del negocio"). Una instruccion interna filtrandose a la salida es peor que el
    # problema que venia a arreglar. Va aca, como limite de lo que se escribe.
    "- NUNCA escribas 'link' ni 'enlace' en `errores` ni en `recomendacion` cuando el motivo "
    "sea registro: el alta se hace en el chat y un link no viene al caso. Y tampoco escribas "
    "que falta aclararle al cliente que no existe un link: eso no es un consejo, es ruido. "
    "Si al operador le falto algo en un registro, es pedir los datos o explicar como sigue.\n"
    "- rating_rationale: que paso en esta sesion, en 2-4 frases, y por que la atencion "
    "merece esa valoracion. MISMA PROHIBICION que las dimensiones: sin 'pero', 'aunque', "
    "'sin embargo', 'falto' ni 'no completo/confirmo/guio'. Este texto se muestra JUNTO a la "
    "estrella, asi que un 'pero' lo convierte en una acusacion al lado de una nota alta. "
    "Los peros van en `recomendacion`, que es el campo hecho para eso. Y tiene que ser "
    "COHERENTE con los HECHOS booleanos que reportaste: si dijiste que atendio el motivo, "
    "el rationale no puede decir que no lo atendio."
)

_MOTIVO_JSON_SHAPE = (
    "Responde UNICAMENTE con un objeto JSON valido, sin texto fuera del JSON, con esta "
    "forma EXACTA (los 4 HECHOS son booleanos; NO incluyas rating_label, lo calcula el sistema):\n"
    '{"motivo": "<uno de: ' + "|".join(MOTIVOS_DEL_LLM) + '">, '
    '"dimensions": {"resolucion": "<nota 1 frase>", "iniciativa": "<nota 1 frase>", '
    '"cortesia": "<nota 1 frase>", "errores": ["<codigos E01-E12, o vacio>"]}, '
    '"atendio_el_motivo": <true|false>, '
    '"hizo_accion_extra": <true|false>, '
    '"cortesia_destacada": <true|false>, '
    '"hubo_maltrato_grave": <true|false>, '
    '"claridad": "<claro|confuso|dudoso>", '
    '"cliente_reinsistio": <true|false>, '
    '"rating_rationale": "<2-4 frases especificas de esta sesion>", '
    '"recomendacion": "<1 consejo accionable, o \\"\\" si excelente>", '
    '"recomendacion_practica": "<el codigo B## al que apunta ese consejo, o \\"\\">", '
    '"atencion": "<empujo|pasivo|no_respondio>", '
    '"deposit_observed": <true|false>}'
    + _CAMPOS_CONTRATO
)


def _motivo_tabla_block() -> str:
    """Tabla de motivos para el prompt: 'motivo: PISO = ... UPLIFT = ...' por cada uno.

    Recorre MOTIVOS_DEL_LLM: describirle al modelo una rubrica que no puede elegir solo
    lo confunde -- `redireccion` la decide `respuesta_fue_solo_traspaso` antes de llamarlo.
    """
    lines = []
    for m in MOTIVOS_DEL_LLM:
        spec = get_rubric(m)
        res = next(d for d in spec.dimensions if d.key == spec.dominant)
        upl = next(d for d in spec.dimensions if d.key == spec.uplift)
        lines.append(f"- {m}: PISO = {res.bien}. UPLIFT = {upl.bien}.")
    return "\n".join(lines)


def build_motivo_prompt(
    target_messages: list[dict], thread_context: str, *, deposit_hint: bool = False,
    abandono_hint: bool = False, con_tiempos: bool = False,
) -> tuple[str, str]:
    """Prompt v2: el LLM elige el MOTIVO de la tabla y califica en 2 capas. (system, user).

    Los HINTS son hechos DETERMINISTAS que el modelo no puede verificar leyendo el texto
    (quien mando el comprobante, si el cliente volvio a escribir). No mueven la nota por
    codigo: se le dan al modelo para que juzgue con la informacion completa. Es la
    correccion de rumbo del 2026-08-07: lo determinista aporta hechos, el modelo juzga.
    """
    hints = ""
    if deposit_hint:
        hints += _MOTIVO_HINT
    if abandono_hint:
        hints += _ABANDONO_HINT
    system = _MOTIVO_SYSTEM.format(
        tabla=_motivo_tabla_block(),
        hint=hints,
        ejemplos=formatear_fewshot(),
        json_shape=_MOTIVO_JSON_SHAPE,
    )
    contexto = (thread_context or "").strip() or "(sin visitas previas)"
    user = _USER_TEMPLATE.format(
        contexto=contexto,
        transcript=format_transcript(target_messages, MOTIVOS[0], con_tiempos=con_tiempos),
    )
    return system, user


def build_motivo_schema() -> dict:
    """Esquema de salida del pase v2: motivo + dimensiones uniformes + label unificado."""
    return {
        "type": "object",
        "properties": {
            # MOTIVOS_DEL_LLM y no MOTIVOS: `redireccion` la decidimos nosotros
            # con `connections`, y el modelo no puede verificarla. Ver src/rubrics.py.
            "motivo": {"type": "string", "enum": list(MOTIVOS_DEL_LLM)},
            "dimensions": {
                "type": "object",
                "properties": {
                    "resolucion": {"type": "string"},
                    "iniciativa": {"type": "string"},
                    "cortesia": {"type": "string"},
                    # ENUM CERRADO: el grammar del nivel 2 lo hace imposible de violar, y en el
                    # nivel 1 el prompt lo pide. Ver src/catalogo_atc.py.
                    "errores": {"type": "array",
                                "items": {"type": "string",
                                          "enum": list(CODIGOS_ERROR)}},
                },
                "required": ["resolucion", "iniciativa", "cortesia"],
            },
            # El vacio es VALIDO: cinco estrellas no lleva consejo, y forzar un codigo ahi
            # inventaria una practica incumplida que no existe.
            "recomendacion_practica": {"type": "string",
                                       "enum": [""] + list(CODIGOS_PRACTICA)},
            "atendio_el_motivo": {"type": "boolean"},
            "hizo_accion_extra": {"type": "boolean"},
            "cortesia_destacada": {"type": "boolean"},
            "hubo_maltrato_grave": {"type": "boolean"},
            "claridad": {"type": "string", "enum": ["claro", "confuso", "dudoso"]},
            "cliente_reinsistio": {"type": "boolean"},
            "rating_rationale": {"type": "string"},
            "recomendacion": {"type": "string"},
            "atencion": {"type": "string", "enum": list(ATENCION_LABELS)},
            "deposit_observed": {"type": "boolean"},
        },
        # El LLM emite HECHOS (booleanos); el codigo deriva rating_label (label_from_facts).
        # recomendacion/atencion/deposit_observed son best-effort (no required).
        "required": [
            "motivo", "dimensions", "atendio_el_motivo", "hizo_accion_extra",
            "cortesia_destacada", "hubo_maltrato_grave", "rating_rationale",
        ],
    }
