"""Tests de la señal de abandono tras pedido."""
from src.signals import cliente_abandono_tras_pedido


def _op(body):
    return {"from_me": True, "is_note": False, "body": body, "sent_from": "OPERATOR"}


def _cli(body, media="chat"):
    return {"from_me": False, "is_note": False, "body": body, "media_type": media}


def test_el_caso_del_negocio_operador_ofrece_y_el_cliente_no_vuelve():
    # Ejemplo real (2026-08-07): el operador ofrecio crear la cuenta y no hubo respuesta.
    msgs = [
        _cli("Quiero registrarme y recibir mi Bono de $5 de Freebet"),
        _cli("Hola grasias saludo"),
        _op("Buenas noches mi amigo, como se encuentra el dia de hoy?"),
        _op("Trabajo como agente en Sorti365 y por tu primera recarga tengo una Freebet de $5"),
        _op("¿Te creo un usuario para que juegues y asi te voy explicando paso a paso?"),
        _op("Estoy a la orden siempre. Escribeme de una cuando gustes."),
    ]
    assert cliente_abandono_tras_pedido(msgs) is True


def test_pedir_datos_sin_respuesta_tambien_cuenta():
    msgs = [_cli("quiero una cuenta"),
            _op("pasame tu nombre completo y correo para crearla")]
    assert cliente_abandono_tras_pedido(msgs) is True


def test_un_cierre_normal_NO_es_abandono():
    # El 93% de las sesiones termina con el operador cerrando: si esto diera True, la
    # señal no informaria nada.
    msgs = [_cli("no me llego la recarga"), _op("ya te la acredito"),
            _op("Gracias por preferirnos!")]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_confirmar_una_transaccion_no_es_pedido():
    msgs = [_cli("les mando el comprobante"), _cli("", media="image"), _op("ing")]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_si_el_cliente_SI_contesto_no_hay_abandono():
    msgs = [_cli("quiero una cuenta"),
            _op("¿te creo el usuario?"),
            _cli("si dale")]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_el_pedido_tiene_que_estar_en_el_TRAMO_FINAL():
    # Pregunto, el cliente contesto, y despues cerro: no quedo nada pendiente.
    msgs = [_cli("hola"), _op("¿te creo el usuario?"), _cli("si"),
            _op("listo, usuario ana clave 12345"), _op("Gracias por preferirnos!")]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_sin_mensajes_del_cliente_no_aplica():
    # Eso es prospeccion saliente pura (no_customer_reply), no un abandono.
    assert cliente_abandono_tras_pedido([_op("¿te creo un usuario?")]) is False


def test_sin_mensajes_del_operador_no_aplica():
    assert cliente_abandono_tras_pedido([_cli("hola?")]) is False


def test_la_cortesia_de_cierre_con_signo_no_alcanza():
    # "¿algo mas?" es una formula de cierre, no un pedido pendiente.
    msgs = [_cli("gracias"), _op("¿necesitas algo mas?")]
    assert cliente_abandono_tras_pedido(msgs) is False
