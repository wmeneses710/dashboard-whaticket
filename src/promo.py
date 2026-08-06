"""Rubrica DETERMINISTA del motivo `promo`. Sin LLM y sin BD.

`promo` es el UNICO motivo donde el eje de UPLIFT sobrevive, y no por gusto: esta
probado con datos de negocio (2026-08-05). Con empuje + MATERIAL el deposito
posterior sube de 24,9% a 34,1% (+9,2 pp). Empujando SOLO CON PALABRAS baja a 19,1%,
**peor que no hacer nada**. Por eso el material no es un adorno, es la condicion.

Y ese es el problema que la rubrica viene a marcar: medido sobre 424 sesiones (1 por
persona), el **68,2% empuja pero solo el 11,8% manda material**. O sea que ~56% de las
sesiones de promo hacen exactamente la version contraproducente.

ESCALA:
    5  mando MATERIAL y respondio <=5 min   (el mejor escenario probado)
    4  respondio <=2 min, sin material
    3  respondio entre 2 y 15 min, sin material
    2  respondio despues de 15 min
    1  no respondio

EL MATERIAL VALE MAS QUE UN PAR DE MINUTOS: mandarlo a los 4 min es mejor que
contestar de palabra en 1. Es lo unico que la prueba de negocio mostro que mueve la
conversion.

POR QUE EL MATERIAL ES PALANCA DEL 5 Y NO CORTE DEL 4. Si el 4 exigiera material, el
88% de las sesiones caeria debajo y volveriamos a una escala aplastada — el mismo
defecto que el cap de uplift que acabamos de sacar. Como palanca del 5 deja un techo
exigente pero real (11,6% lo alcanza hoy), que es exactamente la conducta que el
negocio quiere multiplicar.

LOS RELOJES: mediana 1,7 min, 56,8% <=2 min, 12,5% entre 5 y 15, 4,7% arriba de 15.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    operator_pushed,
    tiene_reloj,
)

MODELO_DETERMINISTA = "determinista/promo-v1"

AGIL = timedelta(minutes=2)        # respuesta inmediata
RAZONABLE = timedelta(minutes=5)   # hasta aca el material sigue llegando a tiempo
TOLERABLE = timedelta(minutes=15)  # mas que esto, la consulta ya se enfrio

_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class Promo:
    """Nota determinista de una sesion de promo."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None
    material: bool
    empuje: bool


_COACHING = {
    2: "El cliente preguntó por la promo y la respuesta tardó más de 15 minutos. Una "
       "consulta así se enfría rápido.",
    3: "Respondiste, pero fuera de los 2 minutos. En promo la ventana es corta.",
    4: "Falta el material. Explicar la promo solo de palabra convierte menos que no "
       "decir nada; con el flyer o el enlace a la vista, bastante más. Mandalo junto "
       "con la invitación.",
}
_COACHING_1 = "El cliente preguntó por la promo y nadie le respondió."


def _material_del_operador(messages: list[dict]) -> bool:
    """El operador mando algo CONCRETO: un flyer/imagen o un enlace."""
    for m in messages:
        if not _is_operator(m):
            continue
        if is_real_media(m.get("media_type")):
            return True
        if _LINK_RE.search(m.get("body") or ""):
            return True
    return False


def calificar_promo(messages: list[dict]) -> Promo | None:
    """Nota determinista de la sesion. None si no hay reloj para medirla."""
    if not tiene_reloj(messages):
        return None
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    primero_cliente = next((m for m in reales if not m.get("from_me")), None)
    if primero_cliente is None:
        return None
    respuesta = next(
        (m for m in reales
         if _is_operator(m) and m["created_at"] >= primero_cliente["created_at"]), None)
    espera = (respuesta["created_at"] - primero_cliente["created_at"]
              if respuesta else None)
    material = _material_del_operador(reales)
    empuje = operator_pushed(reales)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None:
        return Promo(1, "mala", "El cliente preguntó por la promo y nadie le respondió.",
                     None, material, empuje)
    if espera > TOLERABLE:
        return Promo(2, "deficiente",
                     f"Respondió recién {_mins(espera)} después. Una consulta por una "
                     "promo se enfría rápido.",
                     espera, material, empuje)
    # EL MATERIAL manda, y vale mas que un par de minutos: es lo unico que la prueba
    # de negocio mostro que mueve la conversion. `operator_pushed` NO entra en la
    # condicion: en este motivo es casi tautologico, porque su patron matchea
    # `bono|promo|giros gratis` y hablar de la promo ES el motivo. Por eso el empuje
    # verbal solo no distingue nada (68,2% lo hace) y el material si (11,8%).
    if material and espera <= RAZONABLE:
        return Promo(5, "excelente",
                     f"Respondió en {_mins(espera)} y mandó material concreto — el "
                     "flyer o el enlace —, que es lo que hace que la promo convierta.",
                     espera, True, empuje)
    if espera > AGIL:
        return Promo(3, "aceptable",
                     f"Respondió en {_mins(espera)} y solo de palabra. Se apunta a "
                     "contestar en 2 minutos, o a mandar el material.",
                     espera, material, empuje)
    return Promo(4, "buena",
                 f"Respondió en {_mins(espera)}, pero explicó la promo solo de "
                 "palabra: faltó mandar el flyer o el enlace.",
                 espera, material, empuje)


def score_promo(messages: list[dict]) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM."""
    p = calificar_promo(messages)
    if p is None:
        return None
    return ScoreResult(
        rubric="promo",
        motivo="promo",
        rating_label=p.label,
        stars=p.stars,
        rating_rationale=p.rationale,
        dimensions={
            "espera_respuesta_seg": (int(p.espera.total_seconds())
                                     if p.espera is not None else None),
            "mando_material": p.material,
            "empujo": p.empuje,
        },
        llm_model=MODELO_DETERMINISTA,
        # Unico motivo donde `atencion` significa algo: el empuje ES el eje.
        atencion="empujo" if p.empuje else "pasivo",
        deposit_observed=None,
        floor_applied=False,
        recomendacion="" if p.stars == 5 else (
            _COACHING_1 if p.stars == 1 else _COACHING[p.stars]),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
