"""El ancla de `registro` no puede confundir un FORMULARIO BANCARIO con el traspaso de datos.

`_CEDULA_RE` es `\\b\\d{10}\\b`, y en Ecuador una corrida de 10 digitos es la cedula, pero
tambien el celular Y el numero de cuenta que viaja en el formulario de retiro. Como el ancla
elige el ULTIMO traspaso de datos de la sesion (v12), un pedido de retiro POSTERIOR y sin
ninguna relacion con el alta se volvia el ancla, la ventana juzgada saltaba a esa interaccion
y el alta -que si se habia cerrado dias antes- quedaba invisible.

EL CASO REAL (`bcfc1510`, auditoria del 2026-08-13): Arturo entrega credenciales el 10-ago
("A continuación, le envío su usuario y contraseña ... USUARIO: vivienperdomo"); dos dias
despues llega un pedido de retiro ajeno con el numero de cuenta, se vuelve el ancla, y la fila
sale **2 estrellas** con el rationale "El cliente entregó sus datos pero nunca recibió su
usuario y clave: el alta quedó a medias" -- FALSO, y acusando a una persona con nombre.

POR QUE EL VOCABULARIO BANCARIO Y NO LOS PATRONES DE `retiro.py`: el mensaje real del caso
("60 / Vivien Perdomo Poveda / 0802930271 / Banco del Pichincha / Ahorros") **no contiene las
palabras "retiro" ni "monto"**, asi que ni `_FORMULARIO_RE` ni `_MONTO_RE` lo agarran. Lo que
lo delata es el banco.

MEDIDO el 2026-08-13 sobre la copia: de **70.559 mensajes del cliente con una corrida de 10
digitos, 55.890 (79,2%) traen vocabulario bancario** y solo 6.449 traen un email. El daño
confirmado con nota falsa eran 14 sesiones de 1.704, pero la exposicion del ancla era enorme.

EL EMAIL SIGUE GANANDO SIEMPRE: es el unico campo del formulario de alta que no se puede
confundir con otra cosa. Solo se descarta la corrida de 10 digitos SOLA cuando el mensaje es
claramente un formulario de banco.
"""
from datetime import datetime, timedelta, timezone

from src.registro import score_registro, se_creo_la_cuenta

BASE = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)

DATOS_ALTA = "Vivien Perdomo Poveda vivienperdomo@gmail.com 0802930271"
DATOS_SOLO_CEDULA = "Vivien Perdomo Poveda 0802930271"
CREDENCIALES = ("A continuación, le envío su usuario y contraseña. USUARIO: vivienperdomo "
                "CONTRASEÑA: Sorti2026.")
# El formulario de retiro REAL del caso: sin la palabra "retiro" ni "monto".
FORMULARIO_BANCO = "60\nVivien Perdomo Poveda\n0802930271\nBanco del Pichincha\nAhorros"


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


DOS_DIAS = 2 * 24 * 3600


# La sesion del caso real: alta cerrada, cierre del CRM, y dos dias despues un pedido
# bancario que no tiene nada que ver.
SESION_DEL_CASO = [
    _nota(0, "*Asignado automáticamente* a Arturo"),
    _cli(10, "quiero registrarme"),
    _cli(60, DATOS_ALTA),
    _op(120, CREDENCIALES),
    _nota(180, "Arturo *resuelto* la conversación"),
    _cli(DOS_DIAS, FORMULARIO_BANCO),
    _op(DOS_DIAS + 60, "tu retiro está en proceso"),
    _nota(DOS_DIAS + 120, "Majo *resuelto* la conversación"),
]


def test_el_formulario_bancario_no_secuestra_el_ancla_del_alta():
    r = score_registro(SESION_DEL_CASO)
    assert r is not None
    assert r.dimensions["entrego_credenciales"] is True, r.rating_rationale
    assert r.stars >= 4, r.rating_rationale
    assert "nunca recibió" not in r.rating_rationale


def test_el_formulario_bancario_no_rompe_se_creo_la_cuenta():
    assert se_creo_la_cuenta(SESION_DEL_CASO) is True


def test_el_EMAIL_sigue_valiendo_como_traspaso_aunque_haya_un_banco_al_lado():
    # El email es inequivoco: si esta, es el formulario de alta, pase lo que pase.
    msgs = [
        _cli(10, "quiero registrarme"),
        _cli(60, "vivienperdomo@gmail.com, cuenta del Banco Pichincha para el bono"),
        _op(120, CREDENCIALES),
    ]
    r = score_registro(msgs)
    assert r is not None and r.dimensions["entrego_credenciales"] is True
    assert r.stars >= 4, r.rating_rationale


def test_la_cedula_SOLA_sigue_sirviendo_cuando_no_hay_formulario_bancario():
    # EL GUARD del cambio: no se rompe el camino normal. Muchisimos clientes pasan sus
    # datos sin email, y esa cedula tiene que seguir anclando el alta.
    msgs = [
        _cli(10, "quiero registrarme"),
        _cli(60, DATOS_SOLO_CEDULA),
        _op(120, CREDENCIALES),
    ]
    r = score_registro(msgs)
    assert r is not None and r.dimensions["entrego_credenciales"] is True
    assert r.stars >= 4, r.rating_rationale


def test_un_alta_que_de_verdad_quedo_a_medias_sigue_bajando():
    # LA CONTRACARA: sin este test el cambio podria estar tapando el 2 legitimo.
    msgs = [
        _cli(10, "quiero registrarme"),
        _cli(60, DATOS_SOLO_CEDULA),
        _op(120, "dame un momento que la creo"),
    ]
    r = score_registro(msgs)
    assert r is not None
    assert r.dimensions["entrego_credenciales"] is False
    assert r.stars <= 2, r.rating_rationale


# --- EL NUMERO SUELTO QUE CONTESTA UN PEDIDO BANCARIO -------------------------------
# El arreglo de arriba mira el vocabulario bancario DENTRO del mensaje del cliente, y eso deja
# afuera la variante mas comun: el OPERADOR pide los datos y el cliente contesta SOLO el
# numero. CASO REAL `fda5a4f9` (encontrado el 2026-08-13 auditando el propio arreglo):
#     2026-07-28  OPERADOR: "Estas son tus credenciales / Usuario: alexis478 / Clave: 12345"
#     ... doce dias despues, misma sesion mergeada ...
#     2026-08-09  OPERADOR: "pasame nombre completos de titular, numero de cuenta, banco, cedula"
#     2026-08-09  OPERADOR: "numero de cedula"
#     2026-08-09  CLIENTE : "2101059380"        <- el ancla salta ACA
# La ventana juzgada se recorta al retiro, no ve la entrega de credenciales del 28-jul, y la
# fila sale 2 estrellas con "el alta quedó a medias" -- falso.
# El contexto vive en el mensaje ANTERIOR del operador, asi que hay que mirarlo.

PIDE_DATOS_BANCARIOS = "pasame nombre completos de titular, numero de cuenta, banco, cedula"

SESION_NUMERO_SUELTO = [
    _cli(10, "quiero registrarme"),
    _cli(60, DATOS_SOLO_CEDULA),
    _op(120, CREDENCIALES),
    _nota(180, "Arturo *resuelto* la conversación"),
    # doce días después, un retiro en la misma sesión mergeada
    _cli(12 * 24 * 3600, "quiero retirar"),
    _op(12 * 24 * 3600 + 60, PIDE_DATOS_BANCARIOS),
    _cli(12 * 24 * 3600 + 120, "2101059380"),
    _op(12 * 24 * 3600 + 180, "listo, en proceso"),
]


def test_el_numero_suelto_que_contesta_un_pedido_bancario_no_ancla_el_alta():
    r = score_registro(SESION_NUMERO_SUELTO)
    assert r is not None
    assert r.dimensions["entrego_credenciales"] is True, r.rating_rationale
    assert "nunca recibió" not in r.rating_rationale


def test_el_numero_suelto_SIN_pedido_bancario_delante_sigue_anclando():
    # EL GUARD: el camino normal es el cliente pasando su cédula porque le pidieron los datos
    # del ALTA. Eso no se toca.
    msgs = [
        _cli(10, "quiero registrarme"),
        _op(30, "pasame tus datos para crearte la cuenta"),
        _cli(60, "0802930271"),
        _op(120, CREDENCIALES),
    ]
    r = score_registro(msgs)
    assert r is not None and r.dimensions["entrego_credenciales"] is True
    assert r.stars >= 4, r.rating_rationale
