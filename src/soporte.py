"""Rubrica DETERMINISTA del motivo `soporte_cuenta`. Sin LLM y sin BD.

EL EJE lo cerro el negocio el 2026-08-05: **velocidad por MEDIANA + el INTENTO**.

Se saca la RESOLUCION a proposito. En soporte de cuenta el desenlace casi siempre
ocurre fuera del chat — desbloqueos, verificaciones, el area tecnica — asi que
calificar el resultado seria calificar algo que el operador no controla. Lo que si
controla es contestar rapido y hacer algo concreto.

POR QUE LA MEDIANA Y NO EL PEOR TURNO. El peor turno mide CANTIDAD DE TURNOS, no
lentitud: medido el 2026-08-05, `retiro` con 2,0 turnos daba 63,5% de "peor <=2 min" y
`soporte_cuenta` con 4,5 turnos daba 36,6%, mientras la mediana se mantenia estable
(71-85%) en los seis motivos. Soporte es el motivo con mas ida y vuelta, asi que el
peor turno lo estaria castigando por conversar. (Es el mismo defecto que quedo
anotado para revisar en la rubrica de AGENTE.)

ESCALA:
    5  mediana <=2 min + hizo algo concreto + se aseguro de que no faltara nada
    4  mediana <=2 min + hizo algo concreto
    3  mediana <=5 min
    2  mediana >5 min, o no intento nada
    1  no respondio

MEDIDO sobre 56 sesiones (1 por persona): la mediana de espera por sesion es 1,1 min y
el 76,8% entra en 2 min — el motivo mas rapido de todos. Hacen algo concreto el 53,6%,
escalan el 35,7%, y el 28,6% chequea el cierre, que es la marca mas alta de todos los
motivos. El 2,84 de promedio que tenia con la escala vieja no reflejaba ese trabajo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from statistics import median

from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    operator_asked_anything_else,
    operator_sent_credentials,
    tiene_reloj,
)

MODELO_DETERMINISTA = "determinista/soporte-v1"

AGIL = timedelta(minutes=2)
TOLERABLE = timedelta(minutes=5)

# Un PASO concreto: una instruccion accionable, no "ya lo estamos viendo".
_PASO_RE = re.compile(
    r"ingres[aá]|entr[aá]|prob[aá]|reinicia|borra|limpia|actualiza|descarga|"
    r"toca|hac[eé] click|abr[ií]|us[aá]|escrib[ií]|copia|peg[aá]|revis[aá]",
    re.IGNORECASE)
# Escalar TAMBIEN es intentar: es el techo de lo que el operador puede hacer solo.
_ESCALO_RE = re.compile(
    r"escal\w*|departamento|area encargada|lo estamos revisando|pas[eé] tu caso|"
    r"report\w* al equipo|soporte tecnico|con el encargado",
    re.IGNORECASE)


def _norm(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


@dataclass(frozen=True)
class Soporte:
    """Nota determinista de una sesion de soporte de cuenta."""
    stars: int
    label: str
    rationale: str
    mediana: timedelta | None
    intento: bool
    pregunto_algo_mas: bool


_COACHING = {
    2: "La atención fue lenta o no llegó a nada concreto. Aunque el desbloqueo dependa "
       "de otra área, el cliente tiene que salir con un paso a seguir o con la certeza "
       "de que su caso se escaló.",
    3: "Las respuestas tardaron más de 2 minutos. En soporte el cliente ya viene "
       "trabado: cada espera pesa doble.",
    4: "Antes de cerrar, preguntale si necesita algo más. Es el motivo donde más se "
       "nota, porque muchas veces el problema vuelve.",
}
_COACHING_1 = "El cliente reportó un problema con su cuenta y nadie le respondió."


def esperas_por_turno(messages: list[dict]) -> list[timedelta]:
    """Cuanto tardo el operador en CADA turno del cliente.

    Un turno = una corrida de mensajes del cliente hasta la siguiente respuesta del
    operador. Los mensajes sueltos del cliente dentro de la misma corrida no abren un
    turno nuevo: el reloj arranca en el PRIMERO.
    """
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    out, abierto = [], None
    for m in reales:
        if not m.get("from_me"):
            if abierto is None:
                abierto = m["created_at"]
        elif _is_operator(m) and abierto is not None:
            out.append(m["created_at"] - abierto)
            abierto = None
    return out


def _hubo_intento(messages: list[dict]) -> bool:
    """El operador hizo ALGO: dio un paso, escalo, entrego credenciales o mando material."""
    for m in messages:
        if not _is_operator(m):
            continue
        if is_real_media(m.get("media_type")):
            return True
        cuerpo = _norm(m.get("body"))
        if _PASO_RE.search(cuerpo) or _ESCALO_RE.search(cuerpo):
            return True
    return operator_sent_credentials(messages)


def calificar_soporte(messages: list[dict]) -> Soporte | None:
    """Nota determinista de la sesion. None si no hay reloj para medirla."""
    if not tiene_reloj(messages):
        return None
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    esperas = esperas_por_turno(reales)
    intento = _hubo_intento(reales)
    algo_mas = operator_asked_anything_else(reales)

    if not esperas:
        return Soporte(1, "mala",
                       "El cliente reportó un problema con su cuenta y nadie le respondió.",
                       None, intento, algo_mas)
    med = median(esperas)
    mins = formato_espera(med.total_seconds())

    if not intento:
        return Soporte(
            2, "deficiente",
            f"Contestó — habitualmente en {mins} —, pero el cliente no se llevó nada "
            "concreto: ni un paso a seguir ni la certeza de que su caso se escaló.",
            med, False, algo_mas)
    if med > TOLERABLE:
        return Soporte(2, "deficiente",
                       f"Hizo algo por el caso, pero el cliente esperó {mins} en cada "
                       "ida y vuelta.",
                       med, True, algo_mas)
    if med > AGIL:
        return Soporte(3, "aceptable",
                       f"Atendió el caso, aunque el cliente esperó {mins} en cada ida "
                       "y vuelta. El objetivo son 2 minutos.",
                       med, True, algo_mas)
    if algo_mas:
        return Soporte(
            5, "excelente",
            f"Atendió rápido — {mins} de espera habitual —, le dio una salida concreta "
            "y antes de cerrar se aseguró de que no le faltara nada.",
            med, True, True)
    return Soporte(4, "buena",
                   f"Atendió rápido — {mins} de espera habitual — y le dio una salida "
                   "concreta. Cerró sin preguntar si necesitaba algo más.",
                   med, True, False)


def score_soporte(messages: list[dict]) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM."""
    s = calificar_soporte(messages)
    if s is None:
        return None
    return ScoreResult(
        rubric="soporte_cuenta",
        motivo="soporte_cuenta",
        rating_label=s.label,
        stars=s.stars,
        rating_rationale=s.rationale,
        dimensions={
            "mediana_espera_seg": (int(s.mediana.total_seconds())
                                   if s.mediana is not None else None),
            "hubo_intento": s.intento,
            "pregunto_algo_mas": s.pregunto_algo_mas,
        },
        llm_model=MODELO_DETERMINISTA,
        # El uplift se saco a proposito: al que no puede entrar a su cuenta no se le
        # vende un bono.
        atencion=None,
        deposit_observed=None,
        floor_applied=False,
        recomendacion="" if s.stars == 5 else (
            _COACHING_1 if s.stars == 1 else _COACHING[s.stars]),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
