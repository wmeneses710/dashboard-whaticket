"""Un GRUPO de WhatsApp no es una atencion: no se le pone nota a nadie.

EL DAÑO QUE ESTE TEST CIERRA. El 2026-08-21 `no_agent_reply` dejo de ser un skip y paso a
valer 1 estrella (src/sin_respuesta.py). Medido sobre la copia del 2026-08-24, de las 6
filas con 1 estrella que alcanzo a escribir el worker **4 eran GRUPOS**: spam entrante de
tipsters y vendedores ("CHOLO BETS V.I.P", "RETO ESCALERA VERDE, 13.500 pesos ganados",
listas de pronosticos, un vendedor con 21 medias y "En benta si decea pregunte"). El
operador (Virginia x3, Mel x1) marco `*resuelto*` sin contestar -- que en un grupo es lo
CORRECTO -- y se le cargo la nota mas dura que el sistema emite.

EL DATO YA ESTABA EN EL REPO, en un comentario de web/index.html del 2026-08-13: "de 313
`no_agent_reply`, **160 son GRUPOS de WhatsApp (nadie debe contestar ahi)** y 102 son
personas en las colas de jugador". Cuando el skip se convirtio en nota, esa medicion no se
volvio a mirar.

LA SEÑAL ES `tickets.is_group`, NO UNA REGEX. La trae WhatsApp. Medido sobre la copia:
  - separa las 6 filas de 1 estrella SIN UN ERROR: los 4 falsos son `true`, y los 2
    legitimos `false` (un jugador pidiendo el numero de cuenta del Pichincha para recargar,
    y un comprobante de Banca Movil sin responder).
  - de las 139.708 sesiones pendientes, 611 son de grupo y **578 (94,6%) disparaban la
    estrella**. Un grupo que nadie contesta es la regla, no la falla.
  - ninguna conversacion CON cola es de grupo (3.089 de 3.090 del bucket sin cola lo son).

POR QUE SE SALTEA LA SESION ENTERA Y NO SOLO LA ESTRELLA. Las rubricas juzgan una atencion
uno-a-uno: `deposito` pregunta si SE LE acredito al cliente, `info` si SE LE respondio la
consulta. En un grupo no hay UN cliente del otro lado, asi que cualquier nota -- buena o
mala -- es inventada. Se saltea con causa propia para que siga contando en la cobertura del
tablero, que es la regla de la casa (src/router.py). Cuesta 33 sesiones de grupo donde
alguien del negocio si escribio: no se pierde trabajo real medible, se deja de calificar lo
que la rubrica no sabe leer.
"""
from src.router import decide_eligibility
from src.sessions import evaluate_session
from src.sin_respuesta import score_sin_respuesta


def _msg(from_me, body="hola", *, is_note=False, media_type=None):
    return {"from_me": from_me, "is_note": is_note, "body": body,
            "sent_from": None, "user_id": None, "media_type": media_type}


# --- el gate ----------------------------------------------------------------------

def test_un_grupo_se_saltea_aunque_nadie_haya_contestado():
    """El caso exacto de las 4 filas falsas: mensajes del contacto, cero del negocio."""
    assert decide_eligibility(
        real_message_count=11, customer_message_count=11, business_message_count=0,
        es_grupo=True,
    ) == ("skipped", "grupo_de_whatsapp")


def test_un_grupo_se_saltea_aunque_el_negocio_si_haya_escrito():
    """No es "nadie contesto" lo que lo saltea, es que no hay UN cliente que atender."""
    assert decide_eligibility(
        real_message_count=8, customer_message_count=4, business_message_count=4,
        es_grupo=True,
    ) == ("skipped", "grupo_de_whatsapp")


def test_el_grupo_gana_a_las_demas_causas():
    """Va PRIMERO a proposito: `internal_notes_only` es cierto en un grupo vacio, pero
    "es un grupo" explica la fila y "solo notas internas" no."""
    assert decide_eligibility(
        real_message_count=0, customer_message_count=0, business_message_count=0,
        es_grupo=True,
    ) == ("skipped", "grupo_de_whatsapp")


def test_sin_grupo_no_cambia_nada():
    """Las 2 filas de 1 estrella que SI estaban bien puestas siguen evaluandose."""
    assert decide_eligibility(
        real_message_count=1, customer_message_count=1, business_message_count=0,
        es_grupo=False,
    ) == ("evaluated", None)


def test_el_default_es_no_grupo():
    """Falla del lado seguro: sin el dato NO se saltea nada (la mitad de las sesiones
    pendientes no tiene fila en `tickets`, asi que `is_group` llega NULL)."""
    assert decide_eligibility(
        real_message_count=1, customer_message_count=1, business_message_count=0,
    ) == ("evaluated", None)
    assert decide_eligibility(
        real_message_count=1, customer_message_count=1, business_message_count=0,
        es_grupo=None,
    ) == ("evaluated", None)


# --- el camino de la sesion, que es el que corre en produccion --------------------

def test_la_sesion_de_un_grupo_no_llega_a_la_rubrica():
    """`evaluate_session` es lo que llama el worker (src/worker.py:207). Sin esto el gate
    nuevo no protege nada: la nota se decide despues de esta llamada."""
    msgs = [_msg(False, "*RETO ESCALERA VERDE, 13.500 pesos ganados EN SOLO 3 apuestas*"),
            _msg(False, None, media_type="image"),
            _msg(True, "Mel *resuelto* la conversación", is_note=True)]
    assert evaluate_session(msgs, es_grupo=True)[2:] == ("skipped", "grupo_de_whatsapp")
    # La MISMA sesion sin la marca de grupo sigue valiendo 1 estrella: el cambio no toca
    # la falla real, solo deja de cobrarsela a quien atendio un grupo.
    assert evaluate_session(msgs)[2:] == ("evaluated", None)
    assert score_sin_respuesta(msgs).stars == 1
