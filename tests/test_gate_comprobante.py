"""Tests del GATE del comprobante: el caso en que el cliente NO escribe nada.

Todo PURO, en memoria, sin LLM y sin BD.

EL PROBLEMA (auditoria de la copia de prod, 2026-08-11). `has_recharge_context` exige
que el CLIENTE escriba la razon de recarga, pero el flujo real de altisimo volumen es
que el cliente manda la imagen del comprobante y NO ESCRIBE NADA: el caption modal es
vacio (33.914 imagenes) y el segundo es el que le pone la app del banco ("Enviado desde
mi nueva Banca Movil de Banco Pichincha", 11.270).

Consecuencia medida: de las 5.523 sesiones de `deposito` que caian al pase con LLM CON
comprobante del cliente, 5.521 (99,96%) no tenian keyword del cliente. Al no pasar el
gate, `score_deposito` devolvia None y la nota la ponia el LLM, que no tiene reloj ni
chequea la acreditacion y no tiene techo (PIEZA 3/4/5 son solo de `registro`): el 68,2%
de esas sesiones sacaba 5 estrellas contra el 3,6% de las transacciones reales.

LA SEGUNDA PUERTA. Cuando el cliente no pone texto, la unica corroboracion posible de
que la imagen ERA un comprobante es el OPERADOR. Pero la señal tiene que ser del
ARTEFACTO ("recibi tu comprobante"), NO de la CALIDAD ("lo acredite bien"): si se
exigiera acreditacion, solo los depositos BIEN atendidos entrarian a la rubrica y los
mal atendidos seguirian lavandose en el pase generoso del LLM. Por eso vale el acuse
-el "voy", no el "llego"- que es exactamente el caso de 2 estrellas de la rubrica.
"""
from datetime import datetime, timedelta, timezone

from src.deposito import calificar_deposito, es_transaccion
from src.signals import operator_acuso_comprobante

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _comprobante(minutos, body=""):
    return _cli(minutos, body=body, media="image")


def _op(minutos, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


# Plantillas REALES de la copia, con su volumen tras una imagen sin texto del cliente.
ACUSE_VERIFICANDO = "Estamos verificando tu comprobante. Tu recarga se reflejará en breve. 🍀"
ACUSE_PROCESANDO = ("🔜 Tu solicitud de recarga está siendo procesada💳 En breve tendrás "
                    "tu saldo disponible. ¡Con Sorti, Ganas más! 🍀")
ACREDITA = "¡Gracias por tu recarga! Tu saldo ya está disponible. Mucha suerte 🍀"
CAPTION_BANCO = "Enviado desde mi nueva Banca Móvil de Banco Pichincha"


# --- la señal del operador, aislada -------------------------------------------

def test_las_plantillas_reales_de_acuse_corroboran_el_comprobante():
    for plantilla in (ACUSE_VERIFICANDO, ACUSE_PROCESANDO, ACREDITA):
        assert operator_acuso_comprobante([_op(1, plantilla)]) is True, plantilla


def test_el_pitch_de_venta_NO_corrobora_un_comprobante():
    # Criterio del 2026-08-06 que NO se toca: la plantilla de venta menciona la recarga
    # en casi toda prospeccion. Habla del dominio pero no acusa nada RECIBIDO -> no
    # alcanza. Sin esto volveriamos a inflar el gate un 41,4%.
    for pitch in ("Registrate, verifica tu cuenta y con tu primera carga comienza a "
                  "disfrutar de todos los beneficios",
                  "Hola, depositas 5 amiga y recibes 5 mas",
                  "Le comento que las cargas y los retiros se pueden realizar por "
                  "medio de transferencias bancarias"):
        assert operator_acuso_comprobante([_op(1, pitch)]) is False, pitch


def test_pedir_el_comprobante_NO_es_acusarlo_recibido():
    # El caso exacto que separa "recibi tu comprobante" de "mandame tu comprobante":
    # si el operador lo PIDE, es que la imagen que llego no era un comprobante.
    for pedido in ("Enviame tu comprobante por favor",
                   "Para acreditarte necesito que me mandes el comprobante"):
        assert operator_acuso_comprobante([_op(1, pedido)]) is False, pedido


def test_un_acuse_generico_sin_dominio_de_recarga_NO_corrobora():
    # "permitame un momento" tras CUALQUIER foto no dice nada de una recarga. El acuse
    # solo, sin el dominio, dejaria pasar cualquier imagen -> el FP que el gate evita.
    assert operator_acuso_comprobante([_op(1, "Permítame un momento por favor")]) is False


def test_solo_cuenta_el_acuse_POSTERIOR_a_la_imagen():
    # Un acuse ANTERIOR habla de otra cosa (o de otro comprobante ya cerrado): no puede
    # corroborar una imagen que todavia no habia llegado.
    msgs = [_op(0, ACUSE_VERIFICANDO), _comprobante(5)]
    assert operator_acuso_comprobante(msgs, desde=BASE + timedelta(minutes=5)) is False
    assert operator_acuso_comprobante(msgs) is True  # sin `desde` no filtra


def test_el_cliente_no_corrobora_su_propio_comprobante():
    # La señal es del OPERADOR. Un cliente escribiendo la plantilla no vale.
    assert operator_acuso_comprobante([_cli(1, ACUSE_VERIFICANDO)]) is False


# --- el gate completo: comprobante MUDO + acuse -------------------------------

def test_comprobante_sin_texto_del_cliente_con_acuse_SI_es_transaccion():
    # El chat real que destapo el bug (session 0003619f, 2026-06-30): el cliente manda
    # la imagen con el caption del banco y nada mas; el operador acusa a los 49s y
    # confirma la acreditacion. Hoy esto NO era transaccion y se iba al LLM.
    msgs = [_comprobante(0, CAPTION_BANCO),
            _op(1, "Buenas noches Pedro 😉"),
            _op(1, ACUSE_VERIFICANDO),
            _op(3, ACREDITA)]
    assert es_transaccion(msgs) is True
    d = calificar_deposito(msgs)
    assert d is not None and d.acredito is True


def test_comprobante_mudo_con_acuse_y_sin_acreditar_saca_2_no_5():
    # LA PRUEBA DE QUE LA SEÑAL NO ES DE CALIDAD: el operador acusa ("en breve") y nunca
    # confirma que la plata entro. Entra a la rubrica y saca 2, que es su nota. Antes se
    # iba al LLM, donde este mismo caso sacaba 4 o 5.
    msgs = [_comprobante(0), _op(1, ACUSE_PROCESANDO)]
    assert es_transaccion(msgs) is True
    d = calificar_deposito(msgs)
    assert d is not None and d.stars == 2 and d.acredito is False


def test_imagen_muda_sin_acuse_del_operador_NO_es_transaccion():
    # Sin texto del cliente y sin corroboracion del operador no hay con que afirmar que
    # la imagen fue un comprobante: cede el turno al LLM en vez de inventar una nota.
    msgs = [_comprobante(0), _op(1, "Hola, en que te puedo ayudar?")]
    assert es_transaccion(msgs) is False
    assert calificar_deposito(msgs) is None


def test_el_camino_viejo_sigue_valiendo_sin_tocar_al_operador():
    # No-regresion: si el CLIENTE pone la razon, el gate pasa como siempre, sin que el
    # operador tenga que decir nada.
    msgs = [_cli(0, "les mando el comprobante de la recarga"), _comprobante(0),
            _op(1, "listo")]
    assert es_transaccion(msgs) is True


def test_sin_reloj_cede_el_turno_aunque_haya_acuse():
    # Guard de src/context.py: el path por conversacion no trae `created_at`. La rubrica
    # mide tiempos -> cede en vez de explotar con KeyError.
    msgs = [{"from_me": False, "is_note": False, "body": "", "media_type": "image"},
            {"from_me": True, "is_note": False, "body": ACUSE_VERIFICANDO,
             "media_type": "chat", "sent_from": "OPERATOR"}]
    assert es_transaccion(msgs) is False
