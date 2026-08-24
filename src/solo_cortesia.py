"""El cliente no planteo nada: se juzga el ESTANDAR DE CIERRE y nada mas. SIN LLM.

QUE SON ESTAS SESIONES. **5.247** donde el cliente solo saludo, solo agradecio o solo mando
un emoji. `signals.client_sin_motivo` las detecta y hasta el 2026-08-21 se salteaban, asi que
desaparecian del denominador: el tablero medaba sobre menos sesiones de las que hubo.

LA SEÑAL ESTA VERIFICADA CONTRA EL MODELO, no solo contra si misma. Medido con
`scripts/bench_sin_motivo.py` sobre una muestra MIXTA de 40 -- mitad estas, mitad con motivo
real, para que un modelo que contestara "sin planteo" a todo no sacara 100% --: `gemma4:12b`
acerto 40/40 en las dos direcciones y coincidio con `client_sin_motivo` en TODAS. Por eso esta
rubrica no llama al LLM: la señal determinista alcanza y ya esta confirmada. (El `qwen3:14b`
de produccion fallo 3 de 40, y las tres por IGNORAR EL MEDIA: el cliente mandaba solo el
comprobante y el modelo decia que no habia planteado nada.)

SE JUZGA UNA SOLA COSA, Y SALE DEL MANUAL. Sin motivo no hay resolucion que evaluar. Lo unico
que el manual pide en esa situacion es el cierre, y lo pide textual:

    "Cuando un cliente responde con un 'Gracias', emojis, stickers u otro mensaje despues de
     haber resuelto el caso y respetado los tiempos de espera, el operador de linea DEBE
     RESPONDER para mantener el estandar de cierre adecuado."
    "Es politica obligatoria del departamento que el ultimo mensaje siempre sea enviado por
     el operador."

El eje es entonces si el cliente quedo con la ultima palabra, que ya mide
`cliente_tuvo_la_ultima_palabra` (v20) con su gate: si el cliente escribio DESPUES de que el
ticket se cerro, el operador ya habia cumplido el procedimiento (/FIN + los 5 minutos) y no se
lo castiga. Medido en v20: el 83% de los casos son asi.

LA ESCALA, Y POR QUE NO ES 5 NI 2.
  4 cuando cumplio. NO 5: no hubo nada excepcional que hacer, y un 5 aca inflaria el tablero
    con sesiones donde no paso nada -- que seria cambiar un sesgo por otro.
  3 cuando el cliente quedo colgado. NO 2: el 2 es donde viven las fallas con algo en juego
    (no confirmar que la plata entro, dejar al cliente sin acceso) y un "gracias" sin
    responder no es de esa familia. El manual lo tipifica como E06 y por eso baja, pero la
    proporcion importa -- este repo ya pago caro por acusaciones desmedidas.
  Medido: el 98,3% de estas sesiones cerro bien, asi que evaluarlas hace honesto el
  denominador sin fabricar acusaciones.

NO DECLARA MOTIVO: no hay ninguno y ponerle uno seria inventarlo. Por eso no aparece en los
cuadros de calidad POR MOTIVO -- correcto -- y si en el total, que es lo que hoy miente.
"""
from __future__ import annotations

from src.catalogo_coaching import consejo_de
from src.scorer import ScoreResult
from src.signals import cliente_tuvo_la_ultima_palabra

MODELO_DETERMINISTA = "determinista/solo-cortesia-v1"

_RATIONALE_OK = (
    "El cliente no planteó nada (solo cortesía) y el operador cerró el chat como "
    "corresponde, con la última palabra de su lado."
)
_RATIONALE_COLGADO = (
    "El cliente no planteó nada, pero su último mensaje quedó sin respuesta y el ticket "
    "siguió abierto."
)
# EL CONSEJO VIVE EN EL CATALOGO (C41, apunta a B12: "cerrar cada chat de forma correcta y
# profesional"). Nacio aca el 2026-08-21, con el catalogo ya cerrado, asi que las filas
# salian con `recomendacion_codigos: []`. Migrado el 2026-08-24 VERBATIM -- no cambia
# ninguna nota. Solo el colgado lleva consejo: el cierre bien hecho no tiene nada que
# mejorar, igual que `excelente`. Ver src/catalogo_coaching.py.
_CONSEJO = consejo_de("solo_cortesia", "aceptable")


def score_solo_cortesia(messages: list[dict], cierre_at) -> ScoreResult | None:
    """La nota de una sesion sin motivo. None si no hay mensajes.

    NO verifica `client_sin_motivo`: el llamador ya lo sabe (es lo que lo trajo hasta aca) y
    repetirlo seria correr dos veces el mismo patron. Mismo criterio que las otras rubricas
    deterministas, que confian en el ruteo.
    """
    if not messages:
        return None
    # `reales` sin las notas: la nota del CRM es `from_me` pero NO es un mensaje al cliente, y
    # contarla apagaria el eje justo en las sesiones que se cierran bien documentadas. Es la
    # misma leccion que ya esta escrita en `cliente_tuvo_la_ultima_palabra` y en
    # src/sin_respuesta.py.
    reales = [m for m in messages if not m.get("is_note")]
    colgado = cliente_tuvo_la_ultima_palabra(reales, cierre_at)
    if colgado:
        stars, label, rationale = 3, "aceptable", _RATIONALE_COLGADO
        consejo = _CONSEJO
    else:
        stars, label, rationale = 4, "buena", _RATIONALE_OK
        consejo = None
    return ScoreResult(
        # Columna legacy human/bot: el negocio escribio (el ruteo lo garantiza), asi que
        # `human` es lo que corresponde.
        rubric="human",
        motivo=None,
        rating_label=label,
        stars=stars,
        rating_rationale=rationale,
        dimensions={"solo_cortesia": True, "cliente_colgado": colgado},
        llm_model=MODELO_DETERMINISTA,
        atencion=None,
        deposit_observed=None,
        floor_applied=False,
        recomendacion=consejo.texto if consejo else "",
        recomendacion_codigos=[consejo.codigo] if consejo else [],
        recomendacion_practica=consejo.practica if consejo else "",
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
