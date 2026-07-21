"""Rubricas de scoring y mapeo determinista etiqueta -> estrella.

Dos rubricas segun QUIEN atendio la conversacion (no por segmento de negocio):
  - human: la atendio un agente (conversations.user_id presente)
  - bot:   la atendio el chatbot (sin agente)

El scoring es HOLISTICO: el LLM lee la conversacion, llena las dimensiones como
evidencia cualitativa y elige UNA sola etiqueta (rating_label). La estrella es
traduccion DETERMINISTA de esa etiqueta (esta tabla, que controlamos nosotros),
NO una salida del modelo -> los LLM clasifican bien pero calibran mal los numeros.

La dimension `dominant` pone el techo: si falla, la etiqueta no puede superar
"deficiente" (esa regla se instruye en el prompt, ver src/prompts.py).
Ver tambien db/scores_schema.sql.
"""
from __future__ import annotations

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
)
_V2_LABELS = ("excelente", "buena", "aceptable", "deficiente", "mala")
_V2_STARS = {"excelente": 5, "buena": 4, "aceptable": 3, "deficiente": 2, "mala": 1}

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
}

RUBRICS: dict[Rubric, RubricSpec] = {"human": HUMAN, "bot": BOT, **MOTIVO_RUBRICS}


def get_rubric(rubric: Rubric) -> RubricSpec:
    """Devuelve la especificacion de la rubrica o falla si no existe."""
    try:
        return RUBRICS[rubric]
    except KeyError:
        raise ValueError(f"rubrica desconocida: {rubric!r} (validas: {sorted(RUBRICS)})")


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
