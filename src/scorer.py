"""Orquesta el scoring SEMANTICO de UNA conversacion.

Arma el prompt (con contexto del hilo), llama al LLM para obtener la
calificacion cualitativa, valida las claves y aplica la estrella determinista
desde la etiqueta. El LLM nunca decide la estrella. La elegibilidad (rubrica,
evaluated/skipped) la decide antes el router; aca ya llega una conversacion
'evaluated'.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.prompts import build_motivo_prompt, build_motivo_schema
from src.rubrics import MOTIVOS, label_from_facts, label_to_stars
from src.signals import (
    agent_confirmation,
    agent_maltrato,
    agent_pushed,
    agent_resolved,
)

# Motivos transaccionales (plata que ENTRA/SALE) donde una confirmacion o el
# comprobante como media del agente ALCANZA el piso de forma INEQUIVOCA. Se excluye
# 'problema' a proposito: ahi "resolucion" es difusa (un "en breve" en un reclamo no
# prueba que se resolvio) y sus errores son de MOTIVO (comprobante mal clasificado
# como problema), no de piso -> eso lo corrige el guard de motivo, no este floor.
_FLOOR_MOTIVOS = frozenset({"deposito", "retiro"})


class LLM(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict | None = ...) -> dict: ...


@dataclass(frozen=True)
class ScoreResult:
    rubric: str
    dimensions: dict
    rating_label: str
    rating_rationale: str
    stars: int
    llm_model: str
    atencion: str | None            # empujo|pasivo|no_respondio; None si el LLM no dio uno valido
    deposit_observed: bool | None   # observacion LLM del deposito (el gate determinista manda)
    motivo: str | None = None       # pase v2: motivo clasificado por el LLM (None en el pase viejo)
    floor_applied: bool = False     # True si un override determinista cambio un HECHO (ver score_by_motivo)
    recomendacion: str = ""         # consejo accionable para el agente (coaching); "" si excelente


def _validate(raw: dict, schema: dict) -> None:
    """Verifica que la salida del LLM tenga las claves del RATING (lo unico duro).

    Reemplaza la garantia que daria el schema-grammar (que no usamos): pedimos la
    forma en el prompt y la validamos aca. `atencion`/`deposit_observed` NO son
    duros: si el LLM los omite o los manda mal, NO descartamos un rating por lo
    demas valido (se degradan a None en score_conversation). Un rating sin esos
    ejes es preferible a dejar la conversacion atascada reintentando para siempre.
    """
    for key in schema["required"]:
        if key not in raw:
            raise ValueError(f"salida del LLM sin la clave requerida: {key!r}")
    dims = raw.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("salida del LLM: 'dimensions' debe ser un objeto")
    for key in schema["properties"]["dimensions"]["required"]:
        if key not in dims:
            raise ValueError(f"salida del LLM: falta la dimension {key!r}")


def _as_bool(v):
    """Parseo tolerante de deposit_observed: el fast path (format=json) NO garantiza
    un bool real. bool('false') seria True -> hay que parsear el string.
    None (no vino) o valor AMBIGUO -> None: no inventamos un False que dispararia un
    deposit_mismatch falso; degradamos igual que atencion fuera del enum."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("true", "1", "si", "sí", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None  # ambiguo ("no sé", "", etc.) -> sin observacion


def score_by_motivo(
    *,
    target_messages: list[dict],
    thread_context: str,
    llm: LLM,
    deposit_hint: bool = False,
) -> ScoreResult:
    """Pase v2: el LLM clasifica el MOTIVO (de la tabla) y califica en 2 capas.

    La estrella sigue siendo determinista (label_to_stars). El motivo elegido define
    la rubrica del rating_label (la escala es unica, asi que cualquier motivo valida
    igual). `deposit_hint` inyecta la senal determinista de comprobante en el prompt.
    """
    system, user = build_motivo_prompt(target_messages, thread_context, deposit_hint=deposit_hint)
    schema = build_motivo_schema()
    raw = llm.chat_json(system, user, schema)
    _validate(raw, schema)

    motivo = raw.get("motivo")
    if motivo not in MOTIVOS:
        raise ValueError(f"motivo invalido del LLM: {motivo!r} (validos: {list(MOTIVOS)})")
    # Guard determinista deposito<->retiro: el deposit_hint viene de un comprobante del
    # CLIENTE (gate en deposits.py), y eso es una RECARGA. En un retiro el comprobante lo
    # manda el AGENTE. Si el LLM confundio y dijo 'retiro' con hint, se corrige a 'deposito'
    # (arregla el confusor mas comun del modelo y evita "Retiro + Recargado" en el dashboard).
    if deposit_hint and motivo == "retiro":
        motivo = "deposito"
    # problema->deposito: un comprobante del cliente ("Abono a deuda") que el AGENTE
    # confirmo ("ing"/"acreditado") es una recarga completada, NO un reclamo. Se exige
    # la confirmacion (a diferencia de retiro) para NO pisar un reclamo genuino de
    # deposito no acreditado, donde el agente no confirmo nada.
    elif deposit_hint and motivo == "problema" and agent_confirmation(target_messages):
        motivo = "deposito"

    # HECHOS del LLM -> etiqueta por CODIGO. El modelo juzga hechos concretos (que hace
    # bien); la regla de 2 capas la aplica label_from_facts (que el modelo aplicaba de
    # forma inestable). 'atendio' ambiguo -> True (no castigar); el resto solo si es True.
    atendio = _as_bool(raw.get("atendio_el_motivo"))
    atendio = True if atendio is None else atendio
    extra = _as_bool(raw.get("hizo_accion_extra")) is True
    cortesia_destacada = _as_bool(raw.get("cortesia_destacada")) is True
    maltrato = _as_bool(raw.get("hubo_maltrato_grave")) is True

    # OVERRIDES deterministas de los HECHOS (la senal dura le gana al modelo):
    resolved = agent_resolved(target_messages)
    override = False
    # transaccional con confirmacion/comprobante del agente -> el piso SIEMPRE esta atendido
    if motivo in _FLOOR_MOTIVOS and resolved and not atendio:
        atendio, override = True, True
    # 'mala' solo con maltrato real: el modelo lo sobre-marca y el maltrato del agente es
    # rarisimo; sin evidencia determinista, se descarta el maltrato -> no cae a 'mala'.
    if maltrato and not agent_maltrato(target_messages):
        maltrato, override = False, True

    label = label_from_facts(
        atendio_motivo=atendio, hizo_accion_extra=extra,
        cortesia_destacada=cortesia_destacada, hubo_maltrato_grave=maltrato,
    )
    stars = label_to_stars(motivo, label)
    rationale = raw.get("rating_rationale", "")
    if override:
        rationale = f"[ajuste determinista de hechos] {rationale}"

    # ATENCION (#5 + señal de resolucion). Si el agente empujo (link/invitacion/bono por
    # recarga) es 'empujo' aunque el LLM lo subvalue; si no, 'no_respondio' es falso cuando
    # el agente confirmo o mando el comprobante -> al menos 'pasivo'.
    atencion = raw.get("atencion")
    if atencion not in schema["properties"]["atencion"]["enum"]:
        atencion = None
    if agent_pushed(target_messages):
        if atencion in ("pasivo", "no_respondio", None):
            atencion = "empujo"
    elif atencion == "no_respondio" and resolved:
        atencion = "pasivo"

    return ScoreResult(
        rubric=motivo,
        motivo=motivo,
        dimensions=raw["dimensions"],
        rating_label=label,
        rating_rationale=rationale,
        stars=stars,
        llm_model=llm.model,
        atencion=atencion,
        deposit_observed=_as_bool(raw.get("deposit_observed")),
        floor_applied=override,
        recomendacion=raw.get("recomendacion") or "",
    )
