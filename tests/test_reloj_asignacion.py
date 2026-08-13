"""El reloj no puede cobrarle al operador el tiempo que el ticket estuvo SIN ASIGNAR.

`calificar_deposito` medía desde el COMPROBANTE DEL CLIENTE hasta el primer mensaje del
operador, sin saber cuándo el CRM le entregó la conversación. Todo lo que el ticket pasó en
la cola se le cobraba a quien lo levantaba.

MEDIDO el 2026-08-13 sobre las 6 filas de `deposito` en 2 estrellas con el rationale
"Confirmó la acreditación, pero tardó N minutos en avisarle": **en 5 de 6 el reloj era casi
todo espera en cola.**

    sesion     operador          reloj total   en cola   reaccion propia
    48c251a2   Anya Alexandra      308,7 min     300,2         8,5
    c324708f   Anya Alexandra      269,7 min     266,7         3,0
    cc996f57   Anya Alexandra      110,3 min     108,9         1,4
    347ffeac   Anya Alexandra       65,3 min      61,1         4,1
    13f5f9da   Maria Jose           33,8 min      33,4         0,4

Cuatro de las cinco son de la misma persona: contestó en 1,4 a 8,5 minutos y cobró 2
estrellas por "tardar". El umbral que decide ese 2 son 5 minutos (`deposito.ACEPTABLE`), y la
cola sola ya se los come.

El mismo artefacto se confirmó en `info` (caso `7a08654d`: "respondió recién 11,3 minutos
después" cuando la operadora respondió en 44 segundos y el resto fue cola).

LA REGLA: **el reloj arranca cuando el operador PUEDE responder**, o sea en el más tardío
entre el comprobante y la asignación. Es la misma idea que ya rige en `espera_efectiva`, que
descuenta el tiempo fuera del horario de atención: no se cobra lo que el operador no controla.
Y el eje ya estaba medido — la observación del 2026-08-06 dice "primer mensaje tras la
asignación sirve como eje (deposito 0,7 min mediana)"; simplemente no se había usado acá.

SIN NOTA DE ASIGNACIÓN NO SE TOCA NADA: se mide desde el comprobante, como antes. No se
inventa un descuento que no se puede probar.
"""
from datetime import datetime, timedelta, timezone

from src.deposito import score_deposito
from src.operators import asignacion_at

BASE = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)   # 10:00 local, en horario


def _cli(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


def _nota(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": True, "body": body, "media_type": None, "sent_from": None}


COMPROBANTE = [_cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image")]
ACREDITA = "listo, tu saldo ya está disponible"


# --- la señal nueva --------------------------------------------------------

def test_asignacion_at_lee_las_dos_notas_del_CRM():
    msgs = [*COMPROBANTE, _nota(600, "*Asignado automáticamente* a Anya Alexandra"),
            _op(660, ACREDITA)]
    assert asignacion_at(msgs) == BASE + timedelta(seconds=600)
    msgs2 = [*COMPROBANTE, _nota(600, "Anya Alexandra *aceptado* la conversación"),
             _op(660, ACREDITA)]
    assert asignacion_at(msgs2) == BASE + timedelta(seconds=600)


def test_asignacion_at_es_None_cuando_no_hay_nota():
    assert asignacion_at([*COMPROBANTE, _op(60, ACREDITA)]) is None


def test_asignacion_at_ignora_el_cierre():
    # `*resuelto*` NO es una asignación: es el final.
    msgs = [*COMPROBANTE, _op(60, ACREDITA), _nota(90, "Anya Alexandra *resuelto* la conversación")]
    assert asignacion_at(msgs) is None


def test_asignacion_at_ignora_la_entrega_ANTERIOR_a_la_ventana():
    # Si el operador ya tenía la conversación cuando llegó el comprobante, no hay cola que
    # descontar: `desde` la deja afuera y el reloj arranca en el comprobante.
    msgs = [_nota(-600, "*Asignado automáticamente* a Anya"), *COMPROBANTE, _op(60, ACREDITA)]
    assert asignacion_at(msgs, desde=BASE) is None
    # sin el piso, la nota se encuentra igual
    assert asignacion_at(msgs) == BASE + timedelta(seconds=-600)


# --- el reloj de deposito --------------------------------------------------

def test_la_espera_en_cola_no_se_le_cobra_al_operador():
    # EL CASO REAL: el ticket estuvo 100 minutos sin asignar y la operadora contestó
    # 3 minutos después de recibirlo. Antes: 103 min -> 2 estrellas "tardó".
    msgs = [
        *COMPROBANTE,
        _nota(6000, "*Asignado automáticamente* a Anya Alexandra"),
        _op(6060, "recibido"),
        _op(6120, ACREDITA),
    ]
    s = score_deposito(msgs, BASE + timedelta(seconds=7000))
    assert s is not None
    assert s.stars >= 4, s.rating_rationale
    assert "tardó" not in s.rating_rationale


def test_la_demora_PROPIA_del_operador_sigue_castigando():
    # EL GUARD: le asignan el ticket enseguida y IGUAL tarda 20 minutos. Eso sí es suyo.
    msgs = [
        *COMPROBANTE,
        _nota(10, "*Asignado automáticamente* a Anya Alexandra"),
        _op(1220, "recibido"),
        _op(1260, ACREDITA),
    ]
    s = score_deposito(msgs, BASE + timedelta(seconds=2000))
    assert s is not None and s.stars == 2, s.rating_rationale
    assert "tardó" in s.rating_rationale


def test_sin_nota_de_asignacion_el_reloj_no_cambia():
    # Compatibilidad: si el CRM no dejó la nota, se mide desde el comprobante como siempre.
    msgs = [*COMPROBANTE, _op(1220, "recibido"), _op(1260, ACREDITA)]
    s = score_deposito(msgs, BASE + timedelta(seconds=2000))
    assert s is not None and s.stars == 2, s.rating_rationale


def test_la_asignacion_previa_al_comprobante_no_regala_nada():
    # El operador YA tenía la conversación cuando llegó el comprobante: el reloj arranca en
    # el comprobante y la demora es entera suya.
    msgs = [
        _nota(-600, "*Asignado automáticamente* a Anya Alexandra"),
        *COMPROBANTE,
        _op(1220, "recibido"), _op(1260, ACREDITA),
    ]
    s = score_deposito(msgs, BASE + timedelta(seconds=2000))
    assert s is not None and s.stars == 2, s.rating_rationale
