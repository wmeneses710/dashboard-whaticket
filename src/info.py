"""Rubrica DETERMINISTA del motivo `info`. Sin LLM y sin BD.

EL EJE lo cerro el negocio el 2026-08-05: "si hay pregunta -> se respondio; si no hay
-> SIN MOTIVO". La segunda mitad de esa regla YA vive en el skip `sin_motivo`
(`src/sessions.py`), asi que toda sesion de `info` que llega hasta aca tiene algo que
responder por construccion. Esta rubrica NO lleva detector de preguntas.

POR QUE NO. El primer intento midio con `client_asked_question`, que busca "?" o
palabras interrogativas, y reportaba que el 53,1% de `info` "no tiene pregunta". No
era ruido: era el detector mirando lo angosto. El cliente plantea sin preguntar —
"mas informacion por favor", "quiero jugar", "estoy interesado", "de q de trata". El
criterio correcto es **"hubo algo que responder"**, que es exactamente el complemento
de `sin motivo`: si lo que dijo el cliente no es pura cortesia, planteo algo.

`info` es el motivo mas simple de todos: no hay comprobante, ni acreditacion, ni
material que exigir, ni conversion que perseguir (el uplift se le saco a proposito el
2026-08-05, junto con derivar, abandono y duplicacion). Lo unico que el operador
controla es contestar, y hacerlo rapido.

ESCALA:
    5  respondio <=2 min + se aseguro de que no faltara nada
    4  respondio <=2 min
    3  respondio entre 2 y 5 min
    2  respondio despues de 5 min
    1  no respondio

UMBRALES sobre 57 sesiones (1 por persona): mediana 1,5 min, 62,5% <=2 min, 26,8%
entre 2 y 5, 8,9% entre 5 y 15. El chequeo de cierre esta en 12,3%, asi que el 5 queda
exigente y raro — que es lo que corresponde a un techo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import _is_operator, operator_asked_and_waited, tiene_reloj
# La espera se mide en HORARIO DE ATENCION (ver src/horario.py): 26 por ciento de los
# deficientes eran clientes que escribieron de madrugada y operadores que contestaron
# ni bien abrio el turno. La noche no es una demora del operador.
from src.horario import espera_efectiva
from src.operators import inicio_del_reloj

MODELO_DETERMINISTA = "determinista/info-v1"

AGIL = timedelta(minutes=2)
TOLERABLE = timedelta(minutes=5)


@dataclass(frozen=True)
class Info:
    """Nota determinista de una sesion de consulta."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None
    pregunto_algo_mas: bool


_COACHING = {
    2: "Quien pregunta todavía está decidiendo si se queda. Conviene responder con lo "
       "que se sabe y completar después, antes que demorar la primera respuesta.",
    3: "El objetivo son 2 minutos para la primera respuesta, aunque sea parcial: quien "
       "consulta está comparando y la demora se nota.",
    4: "Cerrar con \"¿te falta algo más?\" rinde acá: en una consulta suele quedar "
       "una segunda duda sin plantear. Conviene esperar unos 5 minutos antes de cerrar el "
       "ticket: la pregunta solo sirve si el cliente alcanza a responderla.",
}
_COACHING_1 = ("El cliente preguntó y nadie le respondió. Conviene contestar aunque sea "
               "parcialmente: quien consulta todavía está decidiendo si se queda.")


def calificar_info(messages: list[dict], cierre_at=None) -> Info | None:
    """Nota determinista de la sesion. None si no hay reloj para medirla."""
    if not tiene_reloj(messages):
        return None
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    # El reloj arranca en el PLANTEO del cliente, no en el primer mensaje de la
    # sesion: en la prospeccion saliente el operador escribe primero, y ese tiempo
    # no se le puede imputar.
    planteo = next((m for m in reales if not m.get("from_me")), None)
    if planteo is None:
        return None
    respuesta = next(
        (m for m in reales
         if _is_operator(m) and m["created_at"] >= planteo["created_at"]), None)
    # EL RELOJ ARRANCA CUANDO EL OPERADOR PUEDE RESPONDER (ver src/operators.inicio_del_reloj):
    # la espera EN COLA no es suya. Caso real `7a08654d`: "respondio recien 11,3 minutos
    # despues" cuando la operadora contesto en 44 SEGUNDOS y el resto fue cola.
    # Se le pasa `messages` y NO `reales`: el helper busca las NOTAS del CRM, y `reales` las
    # filtra. `info` no acota ventana, asi que la sesion entera es el alcance correcto.
    inicio = inicio_del_reloj(messages, planteo["created_at"])
    espera = (espera_efectiva(inicio, max(respuesta["created_at"], inicio))
              if respuesta else None)
    algo_mas = operator_asked_and_waited(reales, cierre_at)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None:
        return Info(1, "mala", "El cliente preguntó y nadie le respondió.",
                    None, algo_mas)
    if espera > TOLERABLE:
        return Info(2, "deficiente",
                    f"Respondió recién {_mins(espera)} después de la consulta.",
                    espera, algo_mas)
    if espera > AGIL:
        return Info(3, "aceptable",
                    f"Respondió en {_mins(espera)}. El objetivo son 2 minutos.",
                    espera, algo_mas)
    if algo_mas:
        return Info(5, "excelente",
                    f"Respondió en {_mins(espera)} y antes de cerrar se aseguró de que "
                    "el cliente no necesitara nada más.",
                    espera, True)
    return Info(4, "buena",
                f"Respondió en {_mins(espera)}. Cerró sin preguntar si necesitaba "
                "algo más.",
                espera, False)


def score_info(messages: list[dict], cierre_at=None) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM."""
    i = calificar_info(messages, cierre_at)
    if i is None:
        return None
    return ScoreResult(
        rubric="info",
        motivo="info",
        rating_label=i.label,
        stars=i.stars,
        rating_rationale=i.rationale,
        dimensions={
            "espera_respuesta_seg": (int(i.espera.total_seconds())
                                     if i.espera is not None else None),
            "pregunto_algo_mas": i.pregunto_algo_mas,
        },
        llm_model=MODELO_DETERMINISTA,
        # El uplift se saco a proposito de este motivo (decision del 2026-08-05): al
        # que viene a preguntar un horario no se le vende un bono.
        atencion=None,
        deposit_observed=None,
        floor_applied=False,
        recomendacion="" if i.stars == 5 else (
            _COACHING_1 if i.stars == 1 else _COACHING[i.stars]),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
