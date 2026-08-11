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

from src.registro import le_devolvio_la_pelota, nunca_pidio_los_datos

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)


def _cli(seg, body):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(seg, body, ack=None):
    m = {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
         "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}
    if ack is not None:
        m["ack"] = ack
    return m


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


# --- LA CONDICION 2 SE APOYA EN `ack` ---------------------------------------------
# La condicion 2 ("el cliente siguio escribiendo") existia porque, con el cliente ido, no
# se podia separar "el operador no tuvo chance" de "se fue porque no le pidieron nada" — y
# la duda favorecia al operador. `ack` rompe ese empate: si el cliente LEYO los mensajes
# del operador, el operador tuvo su chance y no la uso.
# Medido el 2026-08-11: son 117 sesiones de `registro` mas que entran por esta puerta.

def test_el_cliente_se_fue_pero_LEYO_tambien_penaliza():
    msgs = [_cli(0, "Hola quiero registrarme"),
            _op(30, "Hola! trabajo para Sorti365, tenemos las mejores cuotas", ack=3)]
    assert nunca_pidio_los_datos(msgs) is True


def test_el_cliente_se_fue_y_NO_lo_leyo_sigue_sin_penalizar():
    # Aca la duda sigue favoreciendo al operador: su mensaje nunca llego a la vista.
    msgs = [_cli(0, "Hola quiero registrarme"),
            _op(30, "Hola! trabajo para Sorti365, tenemos las mejores cuotas", ack=2)]
    assert nunca_pidio_los_datos(msgs) is False


def test_sin_ack_el_cliente_ido_sigue_sin_penalizar():
    # No-regresion del criterio viejo cuando la columna no viene.
    msgs = [_cli(0, "Hola quiero registrarme"),
            _op(30, "Hola! trabajo para Sorti365, tenemos las mejores cuotas")]
    assert nunca_pidio_los_datos(msgs) is False


def test_si_leyo_pero_el_operador_SI_fue_al_punto_no_penaliza():
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "dale, pasame tus datos y te creo la cuenta", ack=3)]
    assert nunca_pidio_los_datos(msgs) is False


# --- DEVOLVERLE LA PELOTA AL CLIENTE ES UNA DEFICIENCIA ---------------------------
# Criterio del negocio (2026-08-11): "si el cliente ya dice que quiere registrarse, y el
# operador le pregunta, es una deficiencia". El piso de la rubrica de `registro` es "guia el
# alta paso a paso": repreguntar la intencion que el cliente YA declaro no es guiar, es un
# paso atras. Ojo con la linea fina: `_AL_PUNTO_RE` ya acepta "¿quieres que te CREE la
# cuenta?" (el operador se ofrece a actuar); lo que se penaliza es "¿te animas a registrarte?"
# (la pelota vuelve al cliente).
# Medido: 188 sesiones, nota media 3,43 y 82 de ellas con 4 o 5 estrellas.

def test_el_caso_real_de_gloria_villacis():
    # session 950868b7, 10-ago. El cliente llego por el formulario de Facebook diciendo que
    # queria registrarse; el operador contesto a los 30s preguntandole si queria registrarse
    # y cerro el ticket 41 min despues sin pedirle un solo dato. Salio 5 estrellas.
    msgs = [_cli(0, "Quiero registrarme y recibir mi Bono de $5 de Freebet."),
            _op(30, "Buenas noches mi amiga, te animas a realizar el registro?", ack=2)]
    assert le_devolvio_la_pelota(msgs) is True


def test_las_formas_de_devolver_la_pelota():
    for frase in ("te animas a realizar el registro?",
                  "¿te animás a crear tu cuenta?",
                  "¿quieres registrarte con nosotros?",
                  "¿te gustaria registrarte hoy?",
                  "¿te interesa registrarte en la plataforma?"):
        msgs = [_cli(0, "quiero registrarme"), _op(30, frase)]
        assert le_devolvio_la_pelota(msgs) is True, frase


def test_ofrecerse_a_CREAR_la_cuenta_NO_es_devolver_la_pelota():
    # La distincion que sostiene la regla: el operador se ofrece a ACTUAR. Eso ya es ir al
    # punto y `nunca_pidio_los_datos` lo trata asi desde el 2026-08-07.
    for frase in ("¿quieres que te cree la cuenta?",
                  "¿queres que te registre yo?",
                  "¿quieres que te ayude con el registro?",
                  "te creo un usuario y te voy explicando paso a paso"):
        msgs = [_cli(0, "quiero registrarme"), _op(30, frase)]
        assert le_devolvio_la_pelota(msgs) is False, frase


def test_no_penaliza_si_el_cliente_nunca_pidio_registrarse():
    # Sin intencion declarada, preguntar es lo correcto: es prospeccion, no un paso atras.
    msgs = [_cli(0, "hola, que promos tienen?"),
            _op(30, "¿te animas a registrarte y te doy $5 de freebet?")]
    assert le_devolvio_la_pelota(msgs) is False


def test_no_penaliza_si_despues_SI_fue_al_punto():
    # Repregunto, pero termino pidiendo los datos: el alta avanzo.
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "¿te animas a realizar el registro?"),
            _op(60, "pasame tu nombre y correo asi te creo la cuenta")]
    assert le_devolvio_la_pelota(msgs) is False


def test_no_penaliza_si_el_alta_se_cerro_igual():
    # Mismo guard medido que la 4ta condicion de nunca_pidio_los_datos: el patron tiene
    # falsos negativos, y un alta consumada no se penaliza por estar redactada distinto.
    msgs = [_cli(0, "quiero registrarme"),
            _op(30, "¿te animas a realizar el registro?"),
            _cli(60, "si, mi correo es ana@mail.com y mi cedula 1712345678"),
            _op(90, "listo, tu usuario es ana01 y tu clave 12345")]
    assert le_devolvio_la_pelota(msgs) is False


def test_QUISIERA_registrarme_cuenta_igual_que_quiero():
    # Una sola letra apagaba los dos techos. Caso `e7d9f25a`: el cliente escribe "Quisiera
    # reGistrarme", el LLM lista tres errores ("No se guio el proceso de registro ni se creo
    # la cuenta") y su propio rationale dice "El operador no atendio el motivo principal del
    # cliente, que era registrarse" — y la nota salio 'buena' (4 estrellas), porque el guard
    # nunca se disparo. De 6 sesiones con esa forma, 5 (83%) quedaron en 4 estrellas.
    for frase in ("Quisiera reGistrarme", "quisiera registrarme por favor",
                  "quisiera crear una cuenta", "quisiera abrir mi cuenta"):
        msgs = [_cli(0, frase),
                _op(30, "Hola! trabajo para Sorti365, tenemos las mejores cuotas"),
                _cli(60, "y como es")]
        assert nunca_pidio_los_datos(msgs) is True, frase
