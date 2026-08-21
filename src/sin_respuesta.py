"""El cliente escribio y NADIE del negocio contesto. Nota determinista, SIN LLM.

LO QUE EL SKIP ESTABA ESCONDIENDO. `no_agent_reply` salteaba **1.167 sesiones**, asi que la
peor falla que este sistema puede medir era justamente la unica que no aparecia en ningun
cuadro. Un skip dice "no habia nada que evaluar"; aca habia todo: un cliente esperando.

Y NO ES NEGLIGENCIA PASIVA. Medido el 2026-08-21 sobre 300 de esas sesiones:
    100,0%  tienen notas del CRM
     99,7%  el nombre del operador sale de una nota "<Nombre> *resuelto* la conversacion"
      0,0%  tienen UN SOLO mensaje del negocio
     75,0%  el cliente se quedo con la ultima palabra
**Un operador marco la conversacion como resuelta sin escribirle nunca al cliente.** Es una
accion deliberada y atribuible. Ejemplos reales de la nota: "Mel *resuelto* la conversación",
"*Asignado automáticamente* a Maria Jose" seguido de "Maria Jose *resuelto* la conversación".

EL MANUAL LO TIPIFICA TRES VECES:
    E06  "Cerrar chats sin seguimiento adecuado o sin despedida. Cada conversacion debe
          cerrarse con un mensaje claro, cordial y profesional."
    B10  el minuto de primera respuesta, que el manual fija dos veces (y con su razon: el
         doble check azul ya le marco al cliente que su mensaje fue leido).
         "Es politica obligatoria del departamento que el ultimo mensaje siempre sea enviado
          por el operador."

LA ATRIBUCION ES SOLIDA. La nota del CRM acierta el 99% contra la verdad conocida (ver las
seis puertas de src/operators.py) y ademas apunta a quien EJECUTO el cierre, no a quien
"tenia" la conversacion -- que es la puerta debil, con 91%. Para una FALLA eso importa: no se
le carga a un asignado que nunca la vio, se le carga a quien la cerro.

NO DECLARA MOTIVO. Nadie contesto, asi que no hay conversacion de la que inferirlo y ponerle
uno seria inventarlo. La falla es ANTERIOR a cualquier motivo.

LA CAUSA NO SE PIERDE AL DEJAR DE SER SKIP. El CHECK de `conversation_scores` exige
`skip_reason IS NULL` en las filas evaluadas, asi que la etiqueta desaparece de ese filtro
del tablero -- correcto, porque dejan de ser "sin evaluar". Para que un supervisor las siga
aislando, la razon viaja en `dimensions.sin_respuesta_del_negocio`.
"""
from __future__ import annotations

from src.scorer import ScoreResult

MODELO_DETERMINISTA = "determinista/sin-respuesta-v1"

_RATIONALE = (
    "El cliente escribió y nadie del negocio le respondió: la conversación se cerró sin "
    "contestarle."
)
# El consejo NOMBRA EL HECHO. Una frase de relleno aca seria peor que el silencio, porque es
# la falla mas grave que el sistema mide. Apunta a B10 (los tiempos de respuesta) igual que
# los `_1` de las otras rubricas, que son la misma situacion vista por motivo.
_COACHING = (
    "El cliente quedó sin ninguna respuesta y la conversación se cerró igual. Aunque no se "
    "pueda resolver en el momento, conviene contestar siempre: una línea alcanza, y el "
    "manual pide que el último mensaje lo envíe el operador."
)


def hubo_respuesta_del_negocio(messages: list[dict]) -> bool:
    """Alguien del negocio le escribio AL CLIENTE.

    LA NOTA DEL CRM NO CUENTA, y es la trampa central de este modulo: es `from_me` pero NO es
    un mensaje al cliente. Contarla convertiria justo estas sesiones en "si respondio" -- el
    bug que el skip venia tapando -- porque el 100% de ellas tiene notas. Misma leccion que
    ya esta documentada en `cliente_tuvo_la_ultima_palabra`.

    El BOT si cuenta, por criterio conservador: si el bot contesto, el cliente no quedo sin
    NINGUNA respuesta. Que el bot no sea un merito es otra pregunta y la contesta
    `metrics.hay_persona_del_negocio`.
    """
    return any(m.get("from_me") and not m.get("is_note") for m in messages)


def score_sin_respuesta(messages: list[dict]) -> ScoreResult | None:
    """1 estrella cuando nadie contesto. None si alguien contesto (cede el turno)."""
    if not messages:
        return None
    if hubo_respuesta_del_negocio(messages):
        return None
    # Tiene que haber un cliente esperando: sin mensajes suyos no hay falla, hay una sesion
    # vacia -- y esa la saltea `internal_notes_only`, que es el skip correcto.
    if not any(not m.get("from_me") and not m.get("is_note") for m in messages):
        return None
    return ScoreResult(
        # `rubric` es la columna legacy human/bot: nadie del negocio escribio, asi que no hay
        # lado que rotular, y `human` es el default con el que conviven las demas.
        rubric="human",
        motivo=None,
        rating_label="mala",
        stars=1,
        rating_rationale=_RATIONALE,
        dimensions={"sin_respuesta_del_negocio": True},
        llm_model=MODELO_DETERMINISTA,
        atencion="no_respondio",
        deposit_observed=None,
        floor_applied=False,
        recomendacion=_COACHING,
        claridad="dudoso",
        friccion=False,
        aciertos=[],
    )
