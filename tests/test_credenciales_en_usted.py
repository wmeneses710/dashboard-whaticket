"""La entrega de credenciales EN USTED: "Su usuario es X y la contraseña tambien X".

`CREDENTIALS_PATTERN` reconocia `usuario: valor`, `tu usuario es` y `tus credenciales`.
La forma de USTED faltaba, y es la que usan las agencias: medido el 2026-08-17 sobre la
copia con la corrida v16 completa, **201 de las 501 filas de `registro` en 2 estrellas con
el rationale "El cliente entregó sus datos pero nunca recibió su usuario y clave"
(40,1%) tienen al operador entregandolas, escrito**. La fila afirma lo contrario de su
propio transcript, sobre un operador con nombre.

Casos reales de la copia:
    a7c79fda  "Su usuario es Apunkjuanarias y la contraseña tambien Apunkjuanarias"
    339fb197  "Su usuario es JhonYepez1 y la contraseña tambien JhonYepez1"
    7d92c3a4  "Su usuario es Kleudio y la contraseña Kleudio"
Los tres con `operator_sent_credentials=False` antes de este arreglo.

EL RADIO NO ES SOLO LA NOTA. La misma señal alimenta `se_creo_la_cuenta`, que es lo que
FUERZA el motivo a `registro` cuando el alta se cerro (src/scorer.py). Medido parcheando
solo el patron y volviendo a llamar a la funcion de produccion: **35 filas de jugador
cambian de motivo** — 17 que hoy viven en `promo` (16 de ellas con 5 estrellas), 15 en
`soporte_cuenta` y 3 sueltas. Es la fuga que el comentario de scorer.py ya documentaba
("de 163 altas consumadas, 40 caian en promo") y que seguia abierta por el usted.

EL GUARD QUE NO SE TOCA: pedir no es entregar. "me ayuda con su usuario por favor" y
"cual es su clave" son PEDIDOS del operador y siguen sin contar.
"""
from datetime import datetime, timedelta, timezone

from src.registro import calificar_registro, se_creo_la_cuenta
from src.signals import operator_sent_credentials

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)

DATOS = "juan_apunk@hotmail.com 0984026436 Apunkjuanarias"
PIDE_DATOS = ("Hola para crearte una cuenta me ayudas con tu correo tu numero de "
              "teléfono un nombre de usuario que desees usar")
ENTREGA_USTED = "Su usuario es Apunkjuanarias y la contraseña tambien Apunkjuanarias"


def _cli(minutos, body):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(minutos, body):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "media_type": "chat",
            "sent_from": "OPERATOR"}


# --- la señal ---------------------------------------------------------------------

def test_su_usuario_es_cuenta_como_entrega():
    """El caso a7c79fda, textual."""
    assert operator_sent_credentials([_op(1, ENTREGA_USTED)]) is True


def test_su_clave_y_su_contrasena_tambien_cuentan():
    assert operator_sent_credentials([_op(1, "Su clave es Sorti123")]) is True
    assert operator_sent_credentials([_op(1, "Su contraseña es la misma")]) is True


def test_el_usted_no_rompe_el_guard_de_pedido():
    """Pedir sigue sin ser entregar: es el guard que separa las dos mitades del alta."""
    assert operator_sent_credentials([_op(1, "me ayuda con su usuario por favor")]) is False
    assert operator_sent_credentials([_op(1, "cual es su clave?")]) is False
    assert operator_sent_credentials(
        [_op(1, "indiqueme su usuario para poder revisar la cuenta")]) is False


def test_la_forma_en_tu_sigue_funcionando():
    """El patron viejo no se toca."""
    assert operator_sent_credentials([_op(1, "tu usuario es Kleudio")]) is True
    assert operator_sent_credentials([_op(1, "Usuario: kleudio Clave: Sorti123")]) is True


# --- la rubrica, punta a punta ----------------------------------------------------

def _sesion_del_alta_en_usted():
    """El transcript de a7c79fda: pide los datos, los recibe, entrega en usted."""
    return [
        _cli(0, "crear cuenta"),
        _op(0, PIDE_DATOS),
        _cli(12, DATOS),
        _op(15, "Ya le ayudo con su usuario"),
        _op(16, ENTREGA_USTED),
        _op(16, "https://www.sorti.ec/home"),
        _op(17, "Solo resta verificar la cuenta"),
    ]


def test_la_nota_ya_no_dice_que_nunca_recibio_las_credenciales():
    """El falso mas caro: 2 estrellas afirmando lo contrario del transcript."""
    r = calificar_registro(_sesion_del_alta_en_usted())
    assert r is not None
    assert "nunca recibió su usuario y clave" not in r.rationale
    assert r.entrego is True
    assert r.stars >= 3


def test_el_alta_cerrada_en_usted_fuerza_el_motivo_registro():
    """`se_creo_la_cuenta` es lo que rescata las 35 filas que hoy se van a promo."""
    assert se_creo_la_cuenta(_sesion_del_alta_en_usted()) is True


def test_el_alta_a_medias_sigue_siendo_deficiente():
    """El control: sin entrega, las otras 284 filas de 2 estrellas siguen igual.

    Es el caso c617e689 de la copia: el operador pide el correo y nunca llega.
    """
    sesion = [
        _cli(0, "quiero registrarme"),
        _op(1, PIDE_DATOS),
        _cli(4, DATOS),
        _op(6, "ayudame con tu correo por favor"),
    ]
    r = calificar_registro(sesion)
    assert r is not None
    assert r.entrego is False
    assert r.stars == 2
    assert "nunca recibió su usuario y clave" in r.rationale
