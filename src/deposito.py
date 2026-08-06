"""Rubrica DETERMINISTA del motivo `deposito`. Sin LLM y sin BD.

POR QUE DETERMINISTA. El motivo lo sigue clasificando el modelo — eso exige leer
intencion y es su trabajo irremplazable. La NOTA no: los tres hechos que la definen
son verificables contra los datos, no interpretables.

    el reloj          cuando llego el comprobante y cuando contesto el operador
    la acreditacion   dijo que la plata llego, no que la estaba procesando
    el cierre         chequeo que al cliente no le faltara nada

QUE PROBLEMA RESUELVE. Con la escala generica, `deposito` no calificaba: 86,4% de las
sesiones caian en 3 estrellas, y de 149 transacciones hechas PERFECTAS (respuesta
<=2 min + acreditacion confirmada) 135 quedaban en 3. Hacerlo bien valia +0,13
estrellas contra no hacerlo. Al sacar el cap de uplift la distribucion se dio vuelta
y el 47,5% de los depositos llegaba a 5 SOLO por cortesia — los operadores usan
plantillas calidas por defecto, asi que ser amable era gratis. Ninguna de las dos
escalas medía el trabajo.

LA ESCALA (definida por el negocio el 2026-08-06: "con que se haga bien y rapido es
suficiente"; el comprobante se exige por AUDITORIA y proteccion de la confianza, no
como metrica de satisfaccion del cliente):

    5  acuse <=2 min + confirmo la acreditacion + se aseguro de que no faltara nada
    4  acuse <=2 min + confirmo la acreditacion
    3  confirmo, pero el acuse tardo 2-5 min
    2  el acuse tardo >5 min, o nunca confirmo la acreditacion
    1  ni respondio ni confirmo

UMBRALES, calibrados sobre 1.254 transacciones (1 sesion por persona, jul-ago 2026):
el 78,0% acusa en <=2 min del comprobante y el 76,2% confirma en <=5 min. Los cortes
separan sin ser ni regalados ni imposibles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from src.deposits import has_recharge_context
from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    operator_acreditacion,
    operator_asked_anything_else,
    tiene_reloj,
)

# Sentinela para la columna `llm_model`: no hubo modelo en la nota. Permite separar
# por SQL las filas del path determinista de las del pase con LLM.
MODELO_DETERMINISTA = "determinista/deposito-v1"

AGIL = timedelta(minutes=2)       # <= 2 min -> el acuse fue inmediato
ACEPTABLE = timedelta(minutes=5)  # <= 5 min -> tolerable; mas que eso, no


@dataclass(frozen=True)
class Deposito:
    """Nota determinista de una sesion de deposito."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None      # del comprobante a la primera respuesta del operador
    acredito: bool
    pregunto_algo_mas: bool


_COACHING = {
    2: "Confirmale siempre al cliente que la plata entró. Un \"en breve\" sin cierre lo "
       "deja sin saber si su recarga se acreditó.",
    3: "Tardaste más de 2 minutos en avisar. Aunque no puedas acreditar en el momento, "
       "decile enseguida que ya recibiste el comprobante.",
    4: "Antes de cerrar, preguntale si necesita algo más.",
}
_COACHING_1 = ("El comprobante quedó sin respuesta. En operaciones de caja conviene "
               "contestar siempre, aunque sea con una línea mientras se procesa.")


def _comprobante_del_cliente(messages: list[dict]):
    """Primer comprobante (imagen del CLIENTE) de la sesion. None si no hay."""
    for m in sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"]):
        if not m.get("from_me") and is_real_media(m.get("media_type")):
            return m
    return None


def es_transaccion(messages: list[dict]) -> bool:
    """La sesion es un deposito HECHO, no una consulta sobre depositos.

    Exige las dos cosas: que el CLIENTE haya dado el contexto de recarga y que haya
    mandado el comprobante. Medido sobre 3.539 sesiones con contexto de recarga
    (1 por persona): solo el 35,4% son transacciones; el 64,6% restante pregunta sin
    depositar y el 99,7% de esas no tiene nada que acreditar. Calificar una consulta
    con la vara transaccional castiga al operador por algo que nunca ocurrio.
    """
    if not tiene_reloj(messages):
        return False
    return has_recharge_context(messages) and _comprobante_del_cliente(messages) is not None


def calificar_deposito(messages: list[dict]) -> Deposito | None:
    """Nota determinista de la sesion. None si no es una transaccion de deposito."""
    if not es_transaccion(messages):
        return None
    comprobante = _comprobante_del_cliente(messages)
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    # El reloj arranca en el COMPROBANTE, no en el primer mensaje: la charla previa
    # no es tiempo que el operador le deba al cliente.
    respuesta = next(
        (m for m in reales
         if _is_operator(m) and m["created_at"] > comprobante["created_at"]), None)
    espera = respuesta["created_at"] - comprobante["created_at"] if respuesta else None
    acredito = operator_acreditacion(reales)
    algo_mas = operator_asked_anything_else(reales)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None and not acredito:
        return Deposito(1, "mala", "El cliente mandó el comprobante y nadie le respondió.",
                        None, False, algo_mas)
    if not acredito:
        return Deposito(
            2, "deficiente",
            f"Respondió en {_mins(espera)}, pero nunca le confirmó al cliente que la "
            "plata había entrado: se quedó sin saber si su recarga se acreditó.",
            espera, False, algo_mas)
    if espera is None or espera > ACEPTABLE:
        return Deposito(
            2, "deficiente",
            f"Confirmó la acreditación, pero tardó {_mins(espera)} en avisarle al "
            "cliente que había recibido el comprobante.",
            espera, True, algo_mas)
    if espera > AGIL:
        return Deposito(
            3, "aceptable",
            f"Confirmó la acreditación, pero tardó {_mins(espera)} en el primer aviso. "
            "El objetivo son 2 minutos.",
            espera, True, algo_mas)
    if algo_mas:
        return Deposito(
            5, "excelente",
            f"Avisó en {_mins(espera)} que había recibido el comprobante, le confirmó "
            "al cliente que la plata entró y antes de cerrar se aseguró de que no le "
            "faltara nada.",
            espera, True, True)
    return Deposito(
        4, "buena",
        f"Avisó en {_mins(espera)} que había recibido el comprobante y le confirmó al "
        "cliente que la plata entró. Cerró sin preguntarle si necesitaba algo más.",
        espera, True, False)


def score_deposito(messages: list[dict]) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM.

    None cuando la sesion no es una transaccion de deposito: ahi decide el caller
    (hoy, el pase con LLM), porque una consulta sobre recargas se juzga por si el
    cliente entendio la respuesta, no por un comprobante que nunca existio.
    """
    d = calificar_deposito(messages)
    if d is None:
        return None
    return ScoreResult(
        rubric="deposito",
        motivo="deposito",
        rating_label=d.label,
        stars=d.stars,
        rating_rationale=d.rationale,
        dimensions={
            "espera_acuse_seg": (int(d.espera.total_seconds())
                                 if d.espera is not None else None),
            "acredito": d.acredito,
            "pregunto_algo_mas": d.pregunto_algo_mas,
        },
        llm_model=MODELO_DETERMINISTA,
        # `atencion` es la vara COMERCIAL del jugador (empujar registro/deposito). En
        # deposito el eje comercial se saco a proposito: el cliente ya deposito.
        atencion=None,
        deposit_observed=True,
        floor_applied=False,
        # La recomendacion es EXACTAMENTE el gap hacia el 5. En el mejor escenario
        # queda vacia: no hay nada que corregir.
        recomendacion="" if d.stars == 5 else (
            _COACHING_1 if d.stars == 1 else _COACHING[d.stars]),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
