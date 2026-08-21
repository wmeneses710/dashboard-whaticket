"""Al agente NO se le exige la pregunta de cierre. El manual lo releva, textual.

    "En conversaciones con **agentes**, y debido a que muchos no responden despues de recibir
     la informacion, el operador PUEDE cerrar el chat cuando el caso haya sido resuelto."
    "Para el cierre se utilizara la respuesta rapida /Fin ... posterior al envio de esta
     respuesta rapida esperaremos 5 minutos y en caso de no haber respuesta finalizaremos."
                                        -- manual de ATC, "Comportamiento particular de los agentes"

POR QUE ESTE TEST EXISTE. Al abrir los motivos al segmento `agente` (2026-08-21) las sesiones
de recarga y pago pasan de `agilidad` -- que mide solo el reloj-- a las rubricas
transaccionales. Medido sobre 800 sesiones reales, la nota baja **-0,80** en `deposito` y
**-0,69** en `retiro`. El negocio aprobo la baja **con la condicion de que salga del manual**,
y al aislar las causas resulta que la mayor parte NO sale:

    deposito   con gate de cierre 3.48   sin el 4.02   -> **+0,54 lo produce el gate**
               283 de 522 filas (54,2%) quedan topadas por el
    retiro     con gate de cierre 3.67   sin el 4.06   -> +0,38, 34 de 89 (38,2%)

O sea: de los -0,80, unos 0,54 serian castigar al operador por no preguntar "¿te falta algo
más?" a alguien que el manual dice que se puede cerrar sin esperar respuesta. Los ~0,26 que
quedan SI salen del manual (acusar el comprobante, confirmar que la plata entro) y esos se
aplican.

Medido apagando SOLO la señal del cierre con un monkeypatch del simbolo en el modulo, y
comparando la rubrica contra si misma -- nunca contra la fila guardada, que se calculo con
codigo viejo.
"""
from datetime import datetime, timedelta, timezone

from src.deposito import score_deposito
from src.retiro import score_retiro

BASE = datetime(2026, 3, 10, 15, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media, "sent_from": None,
            "user_id": None, "ack": 3}


def _op(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media, "sent_from": "WEB",
            "user_id": "op1", "ack": 3}


def _recarga_impecable_sin_preguntar():
    """Comprobante, acuse inmediato y confirmacion de que la plata entro -- pero el operador
    NO pregunta si falta algo. Para un jugador eso topa en 4; para un agente no debe topar."""
    return [
        _cli(0, "cargame 50 al usuario juan01"),
        _cli(0, "", media="image"),
        _op(0, "recibido, ya lo reviso"),
        _op(1, "listo, ya tienes tu saldo disponible"),
    ]


def test_al_jugador_se_le_sigue_exigiendo_la_pregunta():
    """El estandar del jugador NO se toca: es la mitad del cambio que hay que proteger."""
    r = score_deposito(_recarga_impecable_sin_preguntar(), None, None)
    assert r is not None
    assert r.stars == 4, "el techo del jugador cambio, y no era parte de este cambio"


def test_al_agente_no_se_le_exige_y_llega_al_cinco():
    """La cita del manual: puede cerrar cuando el caso esta resuelto, sin esperar respuesta."""
    r = score_deposito(_recarga_impecable_sin_preguntar(), None, None, segmento="agente")
    assert r is not None
    assert r.stars == 5


def test_el_default_es_jugador():
    """Los llamadores que ya existen no se tocan."""
    con = score_deposito(_recarga_impecable_sin_preguntar(), None, None)
    explicito = score_deposito(_recarga_impecable_sin_preguntar(), None, None,
                               segmento="jugador")
    assert con.stars == explicito.stars


def test_el_relevo_no_tapa_lo_que_el_manual_SI_exige():
    """No es una amnistia: si no confirmo que la plata entro, el agente tampoco llega al 5.
    Los ~0,26 de baja que si salen del manual se aplican igual."""
    msgs = [
        _cli(0, "cargame 50 al usuario juan01"),
        _cli(0, "", media="image"),
        _op(1, "en breve te confirmo"),   # acusa pero NUNCA confirma la acreditacion
    ]
    r = score_deposito(msgs, None, None, segmento="agente")
    assert r is not None
    assert r.stars <= 3, f"el relevo del cierre no puede tapar la falta de confirmacion: {r.stars}"


def test_el_reloj_del_agente_sigue_contando():
    """Tampoco releva el minuto: es la regla que el manual fija dos veces."""
    msgs = [
        _cli(0, "cargame 50 al usuario juan01"),
        _cli(0, "", media="image"),
        _op(40, "recibido"),
        _op(41, "listo, ya tienes tu saldo disponible"),
    ]
    r = score_deposito(msgs, None, None, segmento="agente")
    assert r is not None
    assert r.stars <= 3, "40 minutos de demora no pueden dar 4 ni 5"


def test_retiro_tambien_releva_el_cierre_al_agente():
    msgs = [
        _cli(0, "necesito un retiro de 200 del usuario pedro22"),
        _op(0, "ya te proceso el retiro estimado"),
        _op(2, "", media="image"),
    ]
    jugador = score_retiro(msgs, None, None)
    agente = score_retiro(msgs, None, None, segmento="agente")
    assert jugador is not None and agente is not None
    assert agente.stars >= jugador.stars
