"""Los 2 estrellas de `soporte_cuenta` que dicen "ni un paso a seguir" con el paso escrito.

La rama "no hubo intento" emite el texto mas fuerte de la rubrica: "el cliente no se llevó
nada concreto: ni un paso a seguir ni la certeza de que su caso se escaló". MEDIDO el
2026-08-17 sobre la corrida v16 completa: de las 571 filas en 2 estrellas, **385 caen por
esa rama, y en 81 de esas (21%) el operador escribio algo que ES un paso** y el vocabulario
no lo reconoce.

LO QUE FALTABA, sacado de los transcripts reales:
    "intente nuevamente y me avisa"            -> `27a70c00`, `554cd765`
    "me envia una captura de lo que le sale"   -> `27a70c00`, `554cd765`
    "Debes comunicarte a este número ..."      -> `067c90eb`

Y EL PISO QUE NO EXISTIA: en 6 de las 385 el CLIENTE cierra diciendo que se resolvio ("Ya
pude gracias", "Ya ingrese"). `cliente_confirmo_resuelto` existe desde v16 y es la evidencia
mas dura que hay, pero vive SOLO en el camino LLM (src/scorer.py) y esta rubrica es
determinista, asi que nunca la veia. Caso `27a70c00`: Michelle pide la captura, diagnostica
el punto al final de la contraseña, dice "intente nuevamente" y el cliente contesta "Ya pude
gracias" -- y la fila afirmaba que no se llevo ni un paso.

EL GUARD QUE SE MANTIENE (leccion de `7d562266`, ya documentada en src/soporte.py): la
plantilla de cierre "Gracias por comunicarte con nosotros" esta en casi toda sesion y NO es
una instruccion. Por eso entra `comunicarte AL <numero>` y no `comunicar` suelto.
"""
from datetime import datetime, timedelta, timezone

from src.soporte import _hubo_intento, calificar_soporte

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

PROBLEMA = "no me abre y coloco las credenciales que tiene desde un inicio"


def _cli(minutos, body=PROBLEMA, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


# --- el vocabulario que faltaba ---------------------------------------------------

def test_intentar_de_nuevo_es_un_paso():
    assert _hubo_intento([_op(1, "intente nuevamente y me avisa")]) is True
    assert _hubo_intento([_op(1, "intenta de nuevo por favor")]) is True
    assert _hubo_intento([_op(1, "intentelo otra vez desde la web")]) is True


def test_pedir_la_captura_es_un_paso():
    """Pedir la evidencia es trabajo de diagnostico, no una espera."""
    assert _hubo_intento([_op(1, "me envia una captura de lo que le sale porfavor")]) is True
    assert _hubo_intento([_op(1, "mandame un pantallazo del error")]) is True


def test_derivar_a_un_numero_es_un_paso():
    """`067c90eb`: el operador da la linea que si puede resolverlo."""
    assert _hubo_intento(
        [_op(1, "Debes comunicarte a este número para que te puedan ayudar 593959803754")]
    ) is True


def test_la_plantilla_de_cierre_sigue_sin_ser_un_paso():
    """El guard de `7d562266`: esta frase esta en casi toda sesion."""
    assert _hubo_intento(
        [_op(1, "Gracias por comunicarte con nosotros, quedamos a la orden")]) is False


def test_la_negacion_sigue_invalidando():
    """El verbo nuevo entra BAJO el guard de negacion que ya existia (ver src/soporte.py):
    la frase tiene "intente" y aun asi no es un paso."""
    assert _hubo_intento(
        [_op(1, "no intente nada todavia, espere a que abra el area tecnica")]) is False


# --- el piso del cliente ----------------------------------------------------------

def _sesion_27a70c00():
    """El transcript real, con los tiempos que dieron mediana 2,1 minutos."""
    return [
        _cli(0, "Buenas noches"),
        _op(1, "Buenas noches 😉"),
        _cli(2, PROBLEMA),
        _cli(2, "Sale que son datos incorrectos"),
        _op(6, "me envia una captura de lo que le sale porfavor"),
        _cli(6, "", media="image"),
        _op(8, "la contraseña no le falta de casualidad algun punto al final ?"),
        _cli(12, "No, sigue saliendo lo mismo"),
        _op(14, "intente nuevamente y me avisa"),
        _cli(15, "Ya pude gracias"),
    ]


def test_el_caso_real_ya_no_dice_que_no_se_llevo_nada():
    s = calificar_soporte(_sesion_27a70c00())
    assert s is not None
    assert s.intento is True
    assert "no se llevó nada concreto" not in s.rationale
    assert s.stars >= 3


def test_el_cliente_confirmando_alcanza_solo():
    """Aunque el operador no escriba un paso reconocible, el cliente es el testigo.

    Caso `8136cbb2`: "No puedo ingresar a mi cuenta" -> saludo -> "ya pude".
    """
    sesion = [
        _cli(0, "Hola, estoy escribiendo desde sorti.ec"),
        _cli(1, "No puedo ingresar a mi cuenta"),
        _op(1, "Buenas noches 😉"),
        _cli(1, "Buenas noches ya pude"),
        _op(2, "Perfecto, un placer atenderte 🫡"),
    ]
    s = calificar_soporte(sesion)
    assert s is not None
    assert s.intento is True
    assert "no se llevó nada concreto" not in s.rationale


def test_sin_paso_y_sin_confirmacion_sigue_siendo_deficiente():
    """El control: las otras 304 filas de la rama no se mueven.

    Caso `75227bc4`: el cliente explica que su celular no tiene camara para el KYC y el
    operador contesta con la promo del primer deposito.
    """
    sesion = [
        _cli(0, "mi movil no tiene camara, no puedo verificar la cuenta"),
        _op(2, "Si mi amigo, para que no se pierda de los tres beneficios que tiene "
               "disponible por su primer recarga"),
    ]
    s = calificar_soporte(sesion)
    assert s is not None
    assert s.intento is False
    assert s.stars == 2
    assert "no se llevó nada concreto" in s.rationale
