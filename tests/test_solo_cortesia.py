"""`sin_motivo` deja de ser un SKIP: se evalua por el estandar de CIERRE. Negocio, 2026-08-21.

QUE SON ESTAS SESIONES. **5.247** donde el cliente no planteo nada: solo saludo, solo
agradecio, solo mando un emoji. `signals.client_sin_motivo` las detecta y hoy se saltean, asi
que desaparecen del denominador -- el tablero mide sobre menos sesiones de las que hubo.

LA SEÑAL ESTA BIEN CALIBRADA, Y SE VERIFICO CONTRA EL MODELO. Medido el 2026-08-21 con
`scripts/bench_sin_motivo.py` sobre una muestra MIXTA de 40 (mitad estas, mitad con motivo
real, para que un modelo que contestara "sin planteo" a todo no sacara 100%): `gemma4:12b`
acerto **40/40** en las dos direcciones y coincidio con `client_sin_motivo` en TODAS. Asi que
esta rubrica no necesita el LLM: la señal determinista alcanza y ya esta confirmada.

QUE SE EVALUA, Y ES UNA SOLA COSA. Si el cliente no trajo un motivo, no hay resolucion que
juzgar. Lo unico que el manual pide en esa situacion es el estandar de cierre, y lo pide
textual:

    "Cuando un cliente responde con un 'Gracias', emojis, stickers u otro mensaje despues de
     haber resuelto el caso y respetado los tiempos de espera, el operador de linea DEBE
     RESPONDER para mantener el estandar de cierre adecuado."
    "Es politica obligatoria del departamento que el ultimo mensaje siempre sea enviado por
     el operador."

O sea: el eje es si el cliente quedo con la ultima palabra. Eso ya lo mide
`cliente_tuvo_la_ultima_palabra` (v20), con su gate de cierre -- si el cliente escribio
DESPUES de que el ticket se cerro, el operador ya habia cumplido el procedimiento y no se lo
castiga.

LA ESCALA, Y POR QUE NO ES 5 NI 2.
  4 "buena" cuando cumplio. NO 5: no hubo nada excepcional que hacer, y un 5 aca inflaria el
    tablero con sesiones donde no paso nada. Es lo que el manual pide, cumplido.
  3 "aceptable" cuando el cliente quedo colgado. NO 2: el 2 es donde viven las fallas con
    algo en juego -- no confirmar que la plata entro, dejar al cliente sin acceso -- y un
    "gracias" sin responder no es de esa familia. El manual lo tipifica (E06) y por eso baja,
    pero la proporcion importa: este repo ya pago caro por acusaciones desmedidas.
  Medido: el **98,3%** de estas sesiones cerro bien, asi que evaluarlas hace honesto el
  denominador sin fabricar acusaciones.

NO DECLARA MOTIVO: no hay ninguno, y ponerle uno seria inventarlo. Por eso no aparece en los
cuadros de calidad POR MOTIVO, que es correcto -- aparece en el total, que es lo que hoy
miente.
"""
from datetime import datetime, timedelta, timezone

from src.solo_cortesia import score_solo_cortesia

BASE = datetime(2026, 3, 10, 17, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="gracias", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media, "sent_from": None,
            "user_id": None, "ack": 3}


def _op(minutos, body="con gusto, a la orden"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat", "sent_from": "WEB",
            "user_id": "op1", "ack": 3}


def _nota(minutos, body="Mel *resuelto* la conversación"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": body, "media_type": None, "sent_from": None,
            "user_id": None, "ack": None}


# --- cumplio el estandar -----------------------------------------------------------------
def test_si_le_contesto_la_cortesia_es_buena():
    r = score_solo_cortesia([_cli(0, "gracias"), _op(1)], None)
    assert r is not None
    assert r.stars == 4
    assert r.rating_label == "buena"


def test_nunca_llega_a_cinco():
    """No hubo nada excepcional que hacer. Un 5 aca infla el tablero con sesiones donde no
    paso nada, y ese es justo el problema que este cambio viene a NO crear."""
    r = score_solo_cortesia([_cli(0, "gracias"), _op(1)], None)
    assert r.stars < 5


# --- lo dejo colgado --------------------------------------------------------------------
def test_si_el_cliente_quedo_con_la_ultima_palabra_baja():
    r = score_solo_cortesia([_op(0, "listo, ya quedo"), _cli(1, "gracias")], None)
    assert r is not None
    assert r.stars == 3
    assert r.rating_label == "aceptable"


def test_no_baja_a_dos_ni_a_uno():
    """La proporcion importa: el 2 es donde viven las fallas con algo en juego. Un "gracias"
    sin responder no es de esa familia."""
    r = score_solo_cortesia([_op(0, "listo, ya quedo"), _cli(1, "gracias")], None)
    assert r.stars > 2


def test_el_gate_del_cierre_exculpa_al_que_cumplio():
    """Si el cliente escribio DESPUES de que el ticket se cerro, el operador ya habia hecho
    el procedimiento (v20: el 83% de los casos son asi). No se lo castiga."""
    cierre = BASE + timedelta(minutes=5)
    r = score_solo_cortesia([_op(0, "listo, ya quedo"), _cli(10, "gracias")], cierre)
    assert r is not None
    assert r.stars == 4


# --- forma del resultado -----------------------------------------------------------------
def test_no_declara_un_motivo_que_no_existe():
    r = score_solo_cortesia([_cli(0, "gracias"), _op(1)], None)
    assert r.motivo is None


def test_la_causa_viaja_en_dimensions():
    """El CHECK de la tabla borra `skip_reason` en las filas evaluadas: la razon tiene que
    quedar en otro lado para que el tablero las siga aislando."""
    r = score_solo_cortesia([_cli(0, "gracias"), _op(1)], None)
    assert r.dimensions.get("solo_cortesia") is True


def test_la_nota_del_crm_no_cuenta_como_respuesta():
    """Misma leccion que en src/sin_respuesta.py y en `cliente_tuvo_la_ultima_palabra`: la
    nota es `from_me` pero NO es un mensaje al cliente. Si contara, una sesion cerrada con
    nota parecería tener respuesta y el eje se apagaria justo donde hay que medirlo."""
    r = score_solo_cortesia([_op(0, "listo"), _cli(1, "gracias"), _nota(2)], None)
    assert r.stars == 3


def test_el_coaching_nombra_el_estandar_de_cierre():
    r = score_solo_cortesia([_op(0, "listo, ya quedo"), _cli(1, "gracias")], None)
    assert r.recomendacion
    bajo = r.recomendacion.lower()
    assert "cierre" in bajo or "último mensaje" in bajo or "ultimo mensaje" in bajo


def test_cuando_cumplio_no_hay_nada_que_aconsejar():
    r = score_solo_cortesia([_cli(0, "gracias"), _op(1)], None)
    assert r.recomendacion == ""


def test_sin_mensajes_no_rompe():
    assert score_solo_cortesia([], None) is None
