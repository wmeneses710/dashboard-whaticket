"""El cliente pidio registrarse y el operador NUNCA le pidio los datos.

MEDIDO el 2026-08-07 sobre 2.549 sesiones donde el CLIENTE pide registrarse explicito:
  - el operador NO va al punto (ni pide datos ni ofrece crear la cuenta) en **972 (38,1%)**
  - con pedido de datos el alta se cierra ~40%; **sin pedido, 12,8%**
  - de esas 972, en **510 (52,5%) el cliente SIGUIO escribiendo**: hubo conversacion viva
    y el pedido nunca llego -> el operador tuvo todas las oportunidades.

La hipotesis original del negocio era la VERBOSIDAD (mucho relleno antes de ir al punto) y
la medicion la desmintio: 0 relleno cerraba 41,5%, 1 mensaje 46,3%, 2-3 mensajes 30,7% pero
4+ subia a 64,9%. Contar mensajes correlaciona con cliente ENGANCHADO, asi que la causalidad
se invierte. Lo que pesa no es cuanto hablo antes: es que nunca lo dijo.

Las CUATRO condiciones (el usuario puso el criterio: "si no hay nada no se podria bajarle
porque no seria justo"):
  1. el cliente pidio registrarse explicitamente
  2. el cliente siguio escribiendo despues (habia con quien hablar)
  3. no hay pedido de datos ni oferta de crear la cuenta
  4. el alta NO se cerro  <- imprescindible: 124 de las 972 cerraron igual, o sea que el
     patron tiene falsos negativos y sin este guard se penalizaria un registro exitoso.
"""
from datetime import datetime, timedelta, timezone

from src.registro import nunca_pidio_los_datos

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)


def _cli(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


# --- el caso que hay que penalizar ------------------------------------------------

def test_puro_pitch_con_el_cliente_presente_SI_penaliza():
    msgs = [_cli(0, "Hola quiero registrarme"),
            _op(30, "Hola! trabajo para Sorti365, una plataforma de apuestas deportivas"),
            _cli(60, "y como es"),
            _op(90, "Tenemos las mejores cuotas del pais y bonos todos los dias"),
            _cli(120, "ah ok")]
    assert nunca_pidio_los_datos(msgs) is True


# --- lo que NO se penaliza -------------------------------------------------------

def test_si_OFRECIO_crear_la_cuenta_no_penaliza():
    # Fue al punto: el ofrecimiento ES el mecanismo de este negocio.
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "¿Te creo un usuario para que juegues?"),
            _cli(60, "si")]
    assert nunca_pidio_los_datos(msgs) is False


def test_si_PIDIO_los_datos_no_penaliza():
    msgs = [_cli(0, "quiero crear una cuenta"),
            _op(30, "Me ayudas con estos datos: correo electronico y numero de celular"),
            _cli(90, "ok")]
    assert nunca_pidio_los_datos(msgs) is False


def test_si_el_cliente_se_FUE_no_penaliza():
    # No se puede separar "no tuvo chance" de "se fue porque no le pidio nada".
    # Por el criterio del negocio, la duda favorece al operador.
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "Hola! trabajo para Sorti365, tenemos los mejores bonos")]
    assert nunca_pidio_los_datos(msgs) is False


def test_si_el_alta_se_CERRO_no_penaliza_aunque_no_matchee_el_patron():
    # El guard de las 124: el operador pidio los datos de una forma que el patron no ve,
    # pero entrego usuario y clave. Penalizar un registro exitoso seria absurdo.
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "dale, decime tu mail y tu celu asi te la armo"),
            _cli(90, "ana@mail.com 0991234567"),
            _op(150, "Listo, tu usuario es anarios y la clave 12345")]
    assert nunca_pidio_los_datos(msgs) is False


def test_sin_pedido_explicito_del_cliente_no_aplica():
    # Si el cliente no pidio registrarse, no hay nada que reprochar.
    msgs = [_cli(0, "cuales son las cuotas de hoy"),
            _op(30, "te paso el listado"),
            _cli(60, "gracias")]
    assert nunca_pidio_los_datos(msgs) is False


def test_sin_mensajes_del_operador_no_aplica():
    # Eso ya lo cubre no_agent_reply.
    assert nunca_pidio_los_datos([_cli(0, "quiero registrarme")]) is False
