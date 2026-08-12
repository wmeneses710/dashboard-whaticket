"""Tests de la señal de abandono tras pedido.

`ack` es el estado de entrega de WhatsApp y viene en `messages` al 100%:
    <0 fallo · 0 pendiente · 1 enviado · 2 entregado · 3 LEIDO · 4 escuchado
Medido el 2026-08-11 sobre los mensajes del operador: 72,4% leidos (ack=3), **25,8%
entregados y NUNCA leidos** (ack=2), 1,2% solo enviados, 0,2% fallidos.
"""
from src.signals import cliente_abandono_tras_pedido


def _op(body, ack=None):
    m = {"from_me": True, "is_note": False, "body": body, "sent_from": "OPERATOR"}
    if ack is not None:
        m["ack"] = ack
    return m


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


# --- PROMETER NO ES PEDIR (el falso positivo de mayor volumen) --------------------
# Hallado el 2026-08-12 auditando el rescore v5, y por DOS auditorias independientes que
# llegaron al mismo regex desde motivos distintos. La rama `env[ií]a\w* (tus|los|el)` del
# patron existe para cazar "enviame el comprobante" (el operador PIDE), pero `\w*` se come
# tambien "te enviaremos el comprobante": la plantilla con la que el operador CONFIRMA un
# retiro. El patron no distinguia "te pedi algo" de "yo te prometo algo".
# TAMAÑO MEDIDO sobre la copia de produccion: 101 de 102 (99,0%) de los `retiro` con la
# señal en true disparaban SOLO por esta frase, y 342 de 377 (90,7%) de los 5 estrellas de
# agilidad. Eso explicaba entera la asimetria que arrastrabamos (24,9% de abandono en
# retiro contra 1,9% en deposito): no era del negocio, era lexico.

def test_prometer_enviar_el_comprobante_NO_es_un_pedido_pendiente():
    # La plantilla real de acuse de retiro. El cliente no tiene nada que contestar: ya
    # tiene su plata. Testigo: cbd2cf33-5504-4b3b-832f-93c051c6bdc7.
    msgs = [_cli("Monto a retirar: $100"),
            _op("Tu retiro está en proceso 🔄. En breve te enviaremos el comprobante de "
                "pago y tu saldo será acreditado", ack=3)]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_las_otras_formas_de_prometer_tampoco_piden():
    for texto in ("te enviaré el comprobante en unos minutos",
                  "le enviamos el comprobante por este medio",
                  "ya le enviaremos los datos de la transferencia",
                  "te enviaríamos el comprobante apenas salga"):
        msgs = [_cli("hice el retiro"), _op(texto, ack=3)]
        assert cliente_abandono_tras_pedido(msgs) is False, texto


def test_pedir_en_imperativo_SIGUE_contando():
    # No hay que perder la señal original: el imperativo dirigido al cliente sigue siendo
    # un pedido pendiente. Es la mitad util de la rama que se acota.
    for texto in ("envíame el comprobante para procesar tu recarga",
                  "envía tus datos completos para crearte la cuenta",
                  "enviar el comprobante correcto por favor"):
        msgs = [_cli("quiero recargar"), _op(texto, ack=3)]
        assert cliente_abandono_tras_pedido(msgs) is True, texto


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


# --- NO SE PUEDE ABANDONAR LO QUE NUNCA SE LEYO (objeto `ack`) --------------------
# La señal existe para NO castigar al operador por un cliente que se fue. Pero "irse" es
# una DECISION del cliente, y no hay decision si el pedido nunca le llego a la vista: ahi
# el mensaje del operador simplemente quedo sin validar, y lo conservador es que el techo
# de la rubrica SI aplique en vez de habilitar la nota maxima.

def test_un_pedido_que_el_cliente_NUNCA_LEYO_no_es_abandono():
    # El caso real que lo destapo (session 950868b7, Gloria Villacis, 10-ago): el cliente
    # pidio registrarse por el formulario de Facebook, el operador contesto a los 30s y
    # cerro 41 min despues. Ese mensaje tiene ack=2: le llego y NO lo abrio nunca.
    msgs = [_cli("Quiero registrarme y recibir mi Bono de $5 de Freebet"),
            _op("Buenas noches mi amiga, te animas a realizar el registro?", ack=2)]
    assert cliente_abandono_tras_pedido(msgs) is False


def test_un_pedido_que_ni_se_entrego_no_es_abandono():
    for ack in (1, 0, -2, -10):
        msgs = [_cli("quiero una cuenta"),
                _op("pasame tu nombre y correo para crearla", ack=ack)]
        assert cliente_abandono_tras_pedido(msgs) is False, ack


def test_un_pedido_LEIDO_y_sin_respuesta_SI_es_abandono():
    # Aca la premisa se cumple entera: el cliente lo vio y no volvio. El operador hizo
    # lo que podia y no se lo capea. (ack=4 es un audio escuchado, tambien cuenta.)
    for ack in (3, 4):
        msgs = [_cli("quiero una cuenta"),
                _op("pasame tu nombre y correo para crearla", ack=ack)]
        assert cliente_abandono_tras_pedido(msgs) is True, ack


def test_sin_ack_se_conserva_el_comportamiento_anterior():
    # Los transcripts que no traen `ack` (path por conversacion, fixtures a mano) no
    # pueden PERDER la señal por una columna ausente: se degrada al comportamiento viejo.
    msgs = [_cli("quiero una cuenta"), _op("pasame tu nombre y correo para crearla")]
    assert cliente_abandono_tras_pedido(msgs) is True


def test_alcanza_con_que_UNO_de_los_pedidos_pendientes_se_haya_leido():
    # El tramo final suele tener varios mensajes seguidos del operador. Si el cliente
    # leyo alguno con un pedido, abandono el pedido.
    msgs = [_cli("hola"),
            _op("¿Te creo un usuario para que juegues?", ack=3),
            _op("pasame tu correo asi lo dejo listo", ack=2)]
    assert cliente_abandono_tras_pedido(msgs) is True


def test_varios_pedidos_pero_ninguno_leido_no_es_abandono():
    msgs = [_cli("hola"),
            _op("¿Te creo un usuario para que juegues?", ack=2),
            _op("pasame tu correo asi lo dejo listo", ack=2)]
    assert cliente_abandono_tras_pedido(msgs) is False
