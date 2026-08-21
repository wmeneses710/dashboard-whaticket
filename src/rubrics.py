"""Rubricas de scoring y mapeo determinista etiqueta -> estrella.

Dos rubricas segun QUIEN atendio la conversacion (no por segmento de negocio):
  - human: la atendio un operador (conversations.user_id presente)
  - bot:   la atendio el chatbot (sin operador)

El scoring es HOLISTICO: el LLM lee la conversacion, llena las dimensiones como
evidencia cualitativa y elige UNA sola etiqueta (rating_label). La estrella es
traduccion DETERMINISTA de esa etiqueta (esta tabla, que controlamos nosotros),
NO una salida del modelo -> los LLM clasifican bien pero calibran mal los numeros.

La dimension `dominant` pone el techo: si falla, la etiqueta no puede superar
"deficiente" (esa regla se instruye en el prompt, ver src/prompts.py).
Ver tambien db/scores_schema.sql.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

Rubric = str  # "human" | "bot"


@dataclass(frozen=True)
class Dimension:
    """Un eje de evaluacion, con ancla de que es 'bien' y que es 'mal'."""

    key: str
    bien: str
    mal: str


@dataclass(frozen=True)
class RubricSpec:
    name: Rubric
    dominant: str                        # dimension del PISO (capa 1): si falla, techo deficiente
    dimensions: tuple[Dimension, ...]
    labels_desc: tuple[str, ...]         # etiquetas de la mejor (5) a la peor (1)
    label_to_stars: dict[str, int]
    uplift: str | None = None            # dimension del UPLIFT (capa 2): sube de aceptable a 4-5


HUMAN = RubricSpec(
    name="human",
    dominant="resolucion",
    dimensions=(
        Dimension("empatia",
                  "reconoce la situacion/emocion del cliente, valida, trato humano",
                  "frio, robotico, ignora el reclamo"),
        Dimension("claridad",
                  "explica claro, sin ambiguedad, info correcta y ordenada",
                  "confuso, contradictorio, con jerga"),
        Dimension("resolucion",
                  "atiende el motivo de ESTA visita y lo hace avanzar",
                  "evade, no responde el punto, deja igual al cliente"),
        Dimension("tono",
                  "profesional, cordial y respetuoso",
                  "seco, cortante o agresivo"),
    ),
    labels_desc=("excelente", "buena", "aceptable", "deficiente", "mala"),
    label_to_stars={"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1},
)

BOT = RubricSpec(
    name="bot",
    dominant="cobertura_info",
    dimensions=(
        Dimension("cobertura_info",
                  "da la info que el cliente pide dentro de su alcance",
                  "no responde lo que se pide"),
        Dimension("capacidad_enganche",
                  "entiende la intencion, evita loops y respuestas irrelevantes",
                  "loops, no entiende, responde fuera de tema"),
        Dimension("derivacion",
                  "deriva a un humano en el momento justo cuando excede su alcance",
                  "no deriva cuando debia, o deriva de mas sin intentar"),
    ),
    labels_desc=("optima", "funcional", "mejorable", "deficiente", "falla"),
    label_to_stars={"optima": 5, "funcional": 4, "mejorable": 3, "deficiente": 2, "falla": 1},
)

# --- Modelo v2: rubricas por MOTIVO (ver docs/diseno-scoring-v2.md) --------------
# Escala unificada. La nota tiene DOS CAPAS: PISO (dimension `resolucion`, dominant) =
# 3 aceptable si atendio el motivo aunque sea minimo/templateado; UPLIFT (dimension
# `iniciativa` + atencion) sube a 4-5. La eleccion de rubrica pasa a ser por MOTIVO,
# no por handler (human/bot), en el rewire de prompts/router (unidades siguientes).
Motivo = str
MOTIVOS: tuple[Motivo, ...] = (
    "deposito", "retiro", "soporte_cuenta", "info", "promo", "registro", "problema",
    # `redireccion` entro el 2026-08-20 (decision del negocio). Era un SKIP condicionado
    # desde el 2026-08-07: el traspaso puro a una linea viva se salteaba para no castigar
    # al operador por una migracion que decidio el negocio. El skip protegia bien pero
    # BORRABA el traspaso del tablero -- no se podia contar ni comparar entre operadores.
    # Su nota la pone SIEMPRE la rubrica determinista (src/redireccion.score_redireccion):
    # el camino generico del LLM es justo el que le ponia 2 estrellas por "no atendio el
    # motivo", que es lo que el skip evitaba. Ver tests/test_redireccion_motivo.py.
    "redireccion",
)

# Los motivos que se le PREGUNTAN al modelo. No son todos, y la diferencia importa.
#
# `redireccion` queda AFUERA a proposito: que la respuesta del negocio haya sido solo un
# traspaso, que el numero de destino sea una linea NUESTRA y que esa linea este CONNECTED
# son tres hechos que el modelo NO PUEDE VERIFICAR leyendo el transcript -- el ultimo vive
# en la tabla `connections`. Ponerlo en el enum invita a que lo elija por parecido de texto
# ("escribime al 099...") en sesiones que no son traspaso, y despues habria que pisarlo.
# Es el mismo criterio de los hints deterministas de `build_motivo_prompt`, al revés:
# cuando el hecho es nuestro, no se pregunta.
#
# REGLA: un motivo entra a MOTIVOS_DEL_LLM solo si se puede decidir LEYENDO la conversacion.
MOTIVOS_DEL_LLM: tuple[Motivo, ...] = tuple(m for m in MOTIVOS if m != "redireccion")
_V2_LABELS = ("excelente", "buena", "aceptable", "deficiente", "mala")
_V2_STARS = {"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1}
# Escala de etiquetas comun a TODOS los motivos (para el enum del schema del pase v2).
MOTIVO_LABELS: tuple[str, ...] = _V2_LABELS

# Cortesia: eje transversal del UPLIFT (mismo en todos los motivos). Se llama
# 'cortesia' y NO 'atencion' para no colisionar con el campo top-level `atencion`
# (empujo/pasivo/no_respondio), que es la clasificacion del esfuerzo del operador.
_CORTESIA_DIM = Dimension(
    "cortesia",
    "saluda, cordial, buena eleccion de palabras, personaliza (usa el nombre)",
    "seco, sin saludo, cortante o robotico",
)


def _motivo_rubric(name, res_bien, res_mal, upl_bien, upl_mal) -> RubricSpec:
    """Arma una RubricSpec de motivo: resolucion (piso) + iniciativa (uplift) + cortesia."""
    return RubricSpec(
        name=name, dominant="resolucion", uplift="iniciativa",
        dimensions=(
            Dimension("resolucion", res_bien, res_mal),
            Dimension("iniciativa", upl_bien, upl_mal),
            _CORTESIA_DIM,
        ),
        labels_desc=_V2_LABELS, label_to_stars=dict(_V2_STARS),
    )


MOTIVO_RUBRICS: dict[Motivo, RubricSpec] = {
    "deposito": _motivo_rubric(
        "deposito",
        "acredita el comprobante y confirma explicito (aunque sea templateado: 'listo/ing')",
        "no confirma, acredita mal o ignora el comprobante",
        "personaliza, menciona bonos a alcanzar, invita al canal, resuelve muy rapido",
        "hace solo el tramite, sin nada extra"),
    "retiro": _motivo_rubric(
        "retiro",
        "procesa el retiro y avisa el comprobante (aunque llegue 'en breve')",
        "no procesa, pide mal los datos o ignora la solicitud",
        "invita a volver a depositar (retencion), personaliza, agiliza",
        "solo procesa, sin retencion ni cortesia extra"),
    "soporte_cuenta": _motivo_rubric(
        "soporte_cuenta",
        "resuelve o guia el tramite de cuenta (contrasena, cambio de cuenta/nombre, KYC)",
        "no resuelve, deja al cliente sin acceso ni proximos pasos",
        "acompana, confirma la solucion, previene el proximo problema",
        "responde lo justo sin asegurar la solucion"),
    "info": _motivo_rubric(
        "info",
        "responde la consulta de forma correcta y completa",
        "responde incompleto, incorrecto o evade",
        "convence y lleva a un deposito/registro concreto",
        "informa sin impulsar ninguna accion"),
    "promo": _motivo_rubric(
        "promo",
        "explica la promo/bono con claridad",
        "no explica o confunde la promo",
        "empuja el registro o deposito concreto para aprovecharla",
        "solo informa la promo sin empujar la conversion"),
    "registro": _motivo_rubric(
        "registro",
        "guia el alta de la cuenta paso a paso",
        "no guia, abandona el alta a medias",
        "cierra el alta y encamina el primer deposito",
        "guia parcial sin cerrar"),
    "problema": _motivo_rubric(
        "problema",
        "resuelve el problema o lo escala/deriva correctamente",
        "no resuelve ni escala, deja el problema abierto",
        "hace seguimiento, se disculpa proactivamente, previene reincidencia",
        "resuelve lo minimo sin seguimiento"),
    # La nota de `redireccion` es 100% determinista, asi que estas descripciones NO le
    # hablan a ningun LLM: existen para que el tablero y `derive_aciertos` tengan las
    # mismas tres dimensiones que los demas motivos y no haya que ramificar por motivo.
    # El eje real es el del manual (E07/B09): a donde lo mandaron.
    "redireccion": _motivo_rubric(
        "redireccion",
        "traspasa a una linea nuestra que esta viva, avisandole al cliente",
        "lo manda a un numero que no existe o a una linea caida: queda a la deriva",
        "ademas de traspasar, deja el caso encaminado",
        "solo traspasa"),
}

RUBRICS: dict[Rubric, RubricSpec] = {"human": HUMAN, "bot": BOT, **MOTIVO_RUBRICS}


def get_rubric(rubric: Rubric) -> RubricSpec:
    """Devuelve la especificacion de la rubrica o falla si no existe."""
    try:
        return RUBRICS[rubric]
    except KeyError:
        raise ValueError(f"rubrica desconocida: {rubric!r} (validas: {sorted(RUBRICS)})")


# Frase por defecto de cada acierto (fallback si el LLM no dejo una nota-evidencia
# de esa dimension). El detalle real deberia ser la nota del LLM (evidencia concreta).
_ACIERTO_DEFAULTS: dict[str, str] = {
    "resolucion": "atendio el motivo del cliente",
    "claridad": "comunico con claridad, sin que el cliente tuviera que adivinar",
    "iniciativa": "fue mas alla del tramite (accion extra del motivo)",
    "cortesia": "trato cordial y personalizado",
}


# Marcas de que la frase CONCEDE algo y despues lo desmiente. Un texto asi no puede ser la
# evidencia de un acierto: el panel de "lo que se hizo bien" terminaba mostrando la critica.
# Medido el 2026-08-07 con el modelo de prod sobre 45 sesiones: 20 (44,4%) caian en esto.
# El arreglo de fondo es el contrato del prompt (la nota de dimension dice SOLO lo hecho);
# esto es la RED por si el modelo desobedece.
_CONTRADICE_RE = re.compile(
    r"\b(pero|aunque|sin embargo|no obstante)\b"
    r"|\bfalt[oó]\b"
    r"|\bno (se )?(complet|confirm|gui|cerr|ofreci|dio|brind|proporcion|solicit|"
    r"proces|resolvi|acredit|entreg|explic|aclar|proporcion)\w*",
    re.IGNORECASE,
)


def _evidencia_limpia(detalle: str, defecto: str) -> str:
    """El detalle del LLM si describe lo HECHO; la frase por defecto si se contradice."""
    if not detalle or _CONTRADICE_RE.search(detalle):
        return defecto
    return detalle


def derive_aciertos(
    *,
    atendio_motivo: bool,
    hizo_accion_extra: bool,
    cortesia_destacada: bool,
    claridad: str = "claro",
    friccion: bool = False,
    dimensions: dict | None = None,
) -> list[dict]:
    """Lista estructurada de lo que se hizo BIEN (espejo de errores[]), derivada de
    los HECHOS. Hibrido: el codigo decide QUE aciertos hay (consistente con la estrella)
    y usa la nota por dimension del LLM como EVIDENCIA (detalle); si falta, cae a una
    frase por defecto.

    Cada acierto: {"clave": <dimension>, "detalle": <evidencia>}.
    - resolucion (piso): solo si atendio, sin friccion y no fue confuso.
    - claridad: solo si fue 'claro' y sin friccion (la friccion contradice la claridad).
    - iniciativa / cortesia: si el hecho de uplift correspondiente es verdadero.
    """
    dims = dimensions or {}
    out: list[dict] = []

    def add(clave: str) -> None:
        detalle = _evidencia_limpia((dims.get(clave) or "").strip(),
                                    _ACIERTO_DEFAULTS[clave])
        out.append({"clave": clave, "detalle": detalle})

    piso_limpio = atendio_motivo and not friccion and claridad != "confuso"
    if piso_limpio:
        add("resolucion")
    if atendio_motivo and claridad == "claro" and not friccion:
        add("claridad")
    if hizo_accion_extra:
        add("iniciativa")
    if cortesia_destacada:
        add("cortesia")
    return out


# =============================================================================
# Formato de los textos que LEE UNA PERSONA.
#
# El `rating_rationale` de las rubricas deterministas se muestra tal cual en el chat
# y como snippet en la lista de interacciones. Decia cosas como "167s (2.8 min)": el
# mismo dato dos veces, con punto decimal (que en español se lee mal) y con la unidad
# pegada al numero. Y "5 pedido(s)", que es notacion de programador.
# =============================================================================

def formato_espera(segundos: float | None) -> str:
    """Una espera, escrita como la escribiria una persona."""
    if segundos is None:
        return "nunca"
    if segundos < 60:
        n = round(segundos)
        return f"{n} segundo" if n == 1 else f"{n} segundos"
    # El plural se decide sobre el numero QUE SE MUESTRA, no sobre los segundos
    # crudos: 89 s se redondea a "1,5" y eso es plural, aunque 89 < 90.
    if segundos < 3600:
        return _con_unidad(segundos / 60, "minuto", "minutos")
    return _con_unidad(segundos / 3600, "hora", "horas")


def _con_unidad(v: float, singular: str, plural_: str) -> str:
    texto = _num(v)
    return f"{texto} {singular if texto == '1' else plural_}"


def _num(v: float) -> str:
    """Un decimal, con coma, y sin el `,0` cuando es redondo."""
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def plural(n: int, singular: str, plural_: str | None = None) -> str:
    """`5 pedidos` / `1 pedido`. Nada de `pedido(s)`."""
    return f"{n} {singular if n == 1 else (plural_ or singular + 's')}"


def label_to_stars(rubric: Rubric, label: str) -> int:
    """Traduce una etiqueta cualitativa a su estrella (1..5), de forma determinista.

    Falla si la etiqueta no pertenece a la rubrica (protege contra un LLM que
    devuelva una etiqueta de la otra rubrica o inventada).
    """
    spec = get_rubric(rubric)
    try:
        return spec.label_to_stars[label]
    except KeyError:
        raise ValueError(
            f"etiqueta {label!r} no valida para rubrica {rubric!r} "
            f"(validas: {list(spec.labels_desc)})"
        )


def label_from_facts(
    *,
    atendio_motivo: bool,
    hizo_accion_extra: bool,
    cortesia_destacada: bool,
    hubo_maltrato_grave: bool,
    claridad: str = "claro",
    friccion: bool = False,
    confuso_corroborado: bool = False,
) -> str:
    """Deriva la etiqueta cualitativa desde HECHOS concretos (2 capas + modulador).

    El LLM juzga los hechos (que hace bien) y el CODIGO aplica la regla (que el
    modelo aplicaba de forma inestable). Reemplaza que el LLM elija rating_label.

    PISO/UPLIFT (capas 1 y 2) + MODULADOR de la CALIDAD del piso (`claridad`,
    `friccion`), que puede bajar un 'atendio' nominal por debajo del piso:
    - maltrato grave                         -> 'mala'       (gatillo de lo peor)
    - NO atendio + friccion (ghosteo total)  -> 'mala'       (cliente rogando, sin respuesta)
    - NO atendio                             -> 'deficiente' (debajo del piso)
    - atendio + friccion                     -> 'deficiente' (el cliente tuvo que
      reinsistir sin respuesta: la friccion real SIEMPRE demota; ya llega gateada
      con `and not resolved` desde el scorer)
    - atendio + claridad 'confuso'           -> 'deficiente' SOLO si esta
      CORROBORADO (`confuso_corroborado`); sin corroboracion, un 'confuso' del LLM
      topa en 'aceptable' (piso cumplido, sin uplift) en vez de hundir la nota.
    - atendio limpio + (extra O cortesia destacada) -> 'excelente' (mejor escenario)
    - atendio limpio (piso)                  -> 'buena'       (se hizo bien)

    ESCALA v4 (definida por el negocio el 2026-08-06), igual para TODOS los motivos:
        5  se logro el MEJOR ESCENARIO del motivo
        4  se hizo bien
        3  falto algo leve
        2  faltaron varias cosas
        1  se demoro mucho Y contesto mal, o no contesto

    QUE CAMBIO Y POR QUE. Hasta v3 el piso limpio topaba en 'aceptable' (3) y para
    pasar de ahi hacia falta el UPLIFT COMERCIAL. Eso convertia al 3 en el default y
    al empuje de venta en un peaje. Medido sobre la tanda del 2026-08-06 (motivo
    `deposito`, 213 sesiones): 149 respondieron en <=2 min Y confirmaron la
    acreditacion — el trabajo completo — y 135 de esas quedaron en 3. Hacerlo
    perfecto valia +0,13 estrellas contra no hacerlo, y solo 5 de 149 llegaban a 5.
    La escala no medía el comportamiento que decía medir. Ahora hacer bien el
    trabajo YA vale 4, y el 3 significa lo que dice la escala: falto algo leve.

    `claridad`: 'claro' | 'confuso' | 'dudoso'. Solo 'confuso' actua (demota si
    esta corroborado, y siempre bloquea el uplift); 'dudoso' es NEUTRAL (borderline
    = no-op: ni baja ni impide subir). `friccion`: senal (determinista + refuerzo
    del LLM) de que el cliente tuvo que reinsistir sin respuesta.
    `confuso_corroborado`: gate para que un 'confuso' del LLM sin corroboracion
    determinista (el cliente ni pregunto ni reinsistio, o el operador resolvio/empujo)
    no hunda la nota a 'deficiente' sin evidencia real de que hizo falta aclarar.
    """
    if hubo_maltrato_grave:
        return "mala"
    if not atendio_motivo:
        # ghosteo total: no atendio Y el cliente reinsistio sin respuesta -> lo peor.
        return "mala" if friccion else "deficiente"
    # la friccion real (ya gateada por el scorer) siempre demota, corroborada o no.
    if friccion:
        return "deficiente"
    if claridad == "confuso":
        # sin corroboracion, el confuso del LLM topa en el piso (sin uplift) en vez
        # de hundir la nota: no hay evidencia determinista de que hizo falta aclarar.
        return "deficiente" if confuso_corroborado else "aceptable"
    # MEJOR ESCENARIO (piso limpio; 'dudoso' no bloquea, solo 'confuso' -ya descartado-).
    # LA CORTESIA NO COMPRA EL 5 (2026-08-14). Hasta aca alcanzaba con `cortesia_destacada`,
    # y es casi gratis: los operadores usan plantillas calidas por defecto -- 212 plantillas
    # globales con mas de 300 usos cada una, la mas repetida con 79.447.
    # YA HABIA PASADO Y ESTA DOCUMENTADO en el docstring de src/deposito.py: la escala vieja
    # se rompio asi, con **el 47,5% de los depositos llegando a 5 SOLO por cortesia**. Las
    # rubricas deterministas se rehicieron para arreglarlo (el unico disparador del 5 en
    # `deposito` es `algo_mas`); el camino LLM quedo con la regla vieja.
    # MEDIDO el 2026-08-14 sobre v15, camino LLM: de 284 'excelente', **130 (46%) no tienen
    # el acierto `iniciativa`** -- registro 30, deposito 40, problema 36, retiro 24.
    # `hizo_accion_extra` describe una ACCION verificable en el transcript; la cortesia
    # describe el TONO, que la plantilla ya trae puesto. La cortesia NO desaparece: sigue
    # produciendo su acierto en `aciertos[]` (ver derive_aciertos), o sea que se reconoce
    # como fortaleza. Lo que deja de hacer es comprar la nota maxima.
    if hizo_accion_extra:
        return "excelente"
    return "buena"
