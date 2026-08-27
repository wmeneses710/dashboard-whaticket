"""Un traspaso se reconoce por el DESTINO, no por como esta redactado.

EL CASO QUE LO TRAJO (2026-08-24): el operador Arturo escribio literalmente
"0959803754 este es mi numero ahora amigo". Ese numero es `ONLY 2`, una linea NUESTRA y
CONNECTED. `tails_del_texto` lo extraia bien y `build_lineas_map` lo tenia, pero
`es_traspaso` devolvia False porque `TRASPASO_PATTERN` exige un VERBO ("escribeme al",
"comunicate al numero", "a partir de ahora..."). Como `traspaso_a_linea_viva` pide las dos
señales en AND y la regex corta primero, el numero no se llegaba a mirar.

POR QUE LA FRASE NO SIRVE COMO DISCRIMINADOR. Medido sobre 45 dias, 41 mensajes con
"comuniquese ... agente" + numero: **28 apuntan a una linea NUESTRA y 13 a un numero AJENO**
-- redireccion y derivacion, dos cosas opuestas con la MISMA redaccion. Y el regex acierta y
falla en los dos grupos por igual: hoy castiga derivaciones legitimas y pierde redirecciones.
Lo que separa las dos NUNCA fue el texto: es a donde apunta el numero.

EL DESTINO ES UNA SEÑAL LIMPIA, MEDIDA. De los 143 mensajes que la regla nueva agrega (linea
nuestra viva, DISTINTA de la del chat, hoy no detectados): **cero falsos positivos claros**.
Se leyeron los 122 textos distintos. Los mas dudosos igual le entregan la linea al cliente.
Tiene sentido: un operador no tiene motivo para tipear el numero de OTRA linea de la empresa
salvo para mandar al cliente ahi. Y los que la regex jamas podria cazar son justamente los
mas comunes -- el operador manda el numero SOLO, sin una palabra ("+593991194133", x10).

LA LINEA PROPIA SE EXCLUYE, y no es un detalle: "te paso mi numero <la misma linea>" es una
DESPEDIDA, no un traspaso. Medidos 38 asi en 45 dias. El template mas frecuente del corpus
("Estoy a la orden siempre. Escribeme de una cuando gustes...", 2.505 veces) es exactamente
eso, y ensanchar la regex a lo bruto los habria convertido a todos en redirecciones.

SIN LINEA PROPIA NO SE COMPARA NADA: Facebook, Instagram y Telegram guardan
`connections.number = NULL`. Ahi manda la regex sola, como antes: falla del lado seguro.
"""
from src.redireccion import es_traspaso, respuesta_fue_solo_traspaso

# `ONLY 2` y `Jugadores PLATAFORMA`, ambas CONNECTED en la data real.
LINEAS = {"959803754": "CONNECTED", "991194133": "CONNECTED",
          "987013562": "DISCONNECTED"}
PROPIA = "991194133"  # la linea en la que esta el chat


def _op(body):
    return {"from_me": True, "is_note": False, "body": body, "sent_from": "OP"}


def test_el_caso_real_de_arturo():
    txt = "0959803754 este es mi numero ahora amigo"
    assert es_traspaso(txt) is False, "sin el destino la frase no alcanza (asi estaba)"
    assert es_traspaso(txt, LINEAS, PROPIA) is True


def test_el_numero_SOLO_tambien_es_traspaso():
    """Es el caso mas frecuente del corpus y el que la regex jamas podria cazar."""
    for txt in ("+593991194133", "0959803754", "593959803754"):
        assert es_traspaso(txt, LINEAS, "984701187") is True, txt


def test_la_MISMA_linea_es_una_despedida_no_un_traspaso():
    txt = "Estoy a la orden siempre. Por aquí te dejo mi número: +593 991194133"
    assert es_traspaso(txt, LINEAS, PROPIA) is False


def test_una_linea_CAIDA_no_cuenta():
    """Mandar a una linea muerta no es traspasar: es dejar al cliente sin a donde ir, y eso
    la rubrica lo juzga distinto (src/redireccion.destino_probadamente_caido)."""
    assert es_traspaso("0987013562", LINEAS, PROPIA) is False


def test_un_numero_AJENO_no_es_traspaso():
    """La derivacion al agente REAL del cliente es el procedimiento correcto del manual.
    Medidos 13 en 45 dias con la misma redaccion que las redirecciones."""
    txt = "0996264150 comuniquese con su agente de confianza estimada"
    assert es_traspaso(txt, LINEAS, PROPIA) is False


def test_sin_mapa_ni_linea_propia_se_comporta_como_antes():
    txt = "0959803754 este es mi numero ahora amigo"
    assert es_traspaso(txt) is False
    assert es_traspaso(txt, LINEAS, None) is True   # sin linea propia igual compara el mapa
    assert es_traspaso(txt, None, PROPIA) is False  # sin mapa no se inventa nada


def test_la_frase_sigue_valiendo_sin_numero():
    """Los wa.link y las migraciones de canal no traen digitos: la regex los cubre."""
    assert es_traspaso("escríbeme al siguiente número: https://wa.link/abc") is True


def test_respuesta_fue_solo_traspaso_usa_el_destino():
    """Es la funcion que decide el MOTIVO en el worker (src/worker.py:293)."""
    msgs = [{"from_me": False, "is_note": False, "body": "hola"},
            _op("0959803754 este es mi numero ahora amigo")]
    assert respuesta_fue_solo_traspaso(msgs) is False
    assert respuesta_fue_solo_traspaso(msgs, LINEAS, PROPIA) is True


def test_si_ademas_ATENDIO_no_es_solo_traspaso():
    """El guard que ya existia no se debilita: si el operador tambien resolvio, el motivo
    real manda y el traspaso es un detalle."""
    msgs = [{"from_me": False, "is_note": False, "body": "no puedo recargar"},
            _op("ya te acredité el saldo"),
            _op("0959803754 este es mi numero ahora amigo")]
    assert respuesta_fue_solo_traspaso(msgs, LINEAS, PROPIA) is False


# --- EL PLUMBING: sin el dato de la BD la regla no dispara nunca -----------------------
# Es la misma leccion que `ack` y `created_at` en tests/test_context.py: la capa pura puede
# estar perfecta y la señal degradarse en silencio porque la columna no viaja.

def test_el_sql_de_pendientes_trae_la_linea_propia():
    from tests.test_worker import _FakeCursor  # noqa: PLC0415

    import src.worker as worker
    cur = _FakeCursor([], description=[])
    worker.fetch_pending_sessions(cur, "datos", 30)
    query, _ = cur.executed[0]
    assert "linea_propia" in query, (
        "sin el numero de la linea del chat no se puede distinguir el traspaso de la "
        "despedida, y el template de despedida es el mas frecuente del corpus")


def test_el_worker_le_pasa_el_mapa_y_la_linea_a_la_deteccion():
    import inspect

    import src.worker as worker
    # El cuerpo se movio a `_score_interaccion_y_persiste` con el grano interaccion
    # (2026-08-27): `score_session_and_store` ahora solo parte la sesion y itera.
    fuente = inspect.getsource(worker._score_interaccion_y_persiste)
    m = [ln for ln in fuente.splitlines() if "respuesta_fue_solo_traspaso" in ln]
    assert m, "cambio la llamada, revisar este test"
    assert "lineas" in m[0], f"la deteccion corre sin el mapa de lineas: {m[0].strip()!r}"


def test_tail_de_normaliza_los_dos_formatos():
    """`0959803754` (local) y `+593 959 803 754` (internacional) son el MISMO numero: los
    ultimos 9 digitos son la parte que comparten con `connections`."""
    from src.redireccion import tail_de

    assert tail_de("0959803754") == "959803754"
    assert tail_de("+593 959 803 754") == "959803754"
    assert tail_de("593959803754") == "959803754"
    assert tail_de(None) is None
    assert tail_de("") is None
    assert tail_de("12345") is None      # muy corto para ser una linea


# --- LAS DOS PUNTAS TIENEN QUE ESTAR DE ACUERDO ----------------------------------------
# El worker RUTEA con `respuesta_fue_solo_traspaso` y despues la rubrica RE-CHEQUEA lo mismo
# adentro de `score_redireccion`. Si una de las dos ve el destino y la otra no, el ruteo
# manda la sesion a `redireccion` y la rubrica cede el turno devolviendo None -- y en esa
# rama del worker no hay fallback, asi que la sesion se queda SIN NOTA.
# Detectado sobre la sesion real `9813f9a2` (la de Arturo, "0959803754 este es mi numero
# ahora amigo"): el ruteo daba True y `score_redireccion` devolvia None.

def test_la_rubrica_ve_el_mismo_traspaso_que_el_ruteo():
    from src.redireccion import score_redireccion

    msgs = [{"from_me": False, "is_note": False, "body": "Aun no tiene whatsap"},
            _op("0959803754 este es mi numero ahora amigo")]
    assert respuesta_fue_solo_traspaso(msgs, LINEAS, PROPIA) is True, "el ruteo lo toma"
    r = score_redireccion(msgs, LINEAS, PROPIA)
    assert r is not None, (
        "el ruteo la manda a redirección y la rúbrica cede el turno: la sesión se queda "
        "sin nota")
    assert r.motivo == "redireccion"


def test_la_rubrica_sigue_cediendo_cuando_NO_es_traspaso_puro():
    """El guard no se debilita: si el operador ademas atendio, no es traspaso puro."""
    from src.redireccion import score_redireccion

    msgs = [{"from_me": False, "is_note": False, "body": "no puedo recargar"},
            _op("ya te acredité"), _op("0959803754")]
    assert score_redireccion(msgs, LINEAS, PROPIA) is None


def test_la_nota_NO_acusa_de_dejar_al_cliente_sin_linea_cuando_SI_la_dejo():
    """REGRESION QUE CASI SE ESCAPA. Al hacer el ruteo consciente del destino, `tiene_destino`
    y `destino_probadamente_caido` seguian con la firma vieja: encontraban CERO mensajes de
    traspaso (la frase no matchea) y la rubrica concluia "lo derivó sin dejarle una línea a
    la que escribir" -> 2 estrellas. El operador HABIA dejado una linea nuestra y VIVA.
    Es la peor familia de bug de este repo: una acusacion falsa, y encima nueva.
    Detectado corriendo la sesion real `9813f9a2`, no por los tests (todos en verde)."""
    from src.redireccion import score_redireccion, tiene_destino

    msgs = [{"from_me": False, "is_note": False, "body": "Aun no tiene whatsap"},
            _op("0959803754 este es mi numero ahora amigo")]
    assert tiene_destino(msgs, LINEAS, PROPIA) is True, "no ve el número que sí está"
    r = score_redireccion(msgs, LINEAS, PROPIA)
    assert "sin dejarle" not in r.rating_rationale, (
        f"acusa de algo que no pasó: {r.rating_rationale!r}")
    assert r.stars >= 3, f"castiga un traspaso bien hecho con {r.stars}★"


# --- UN TRASPASO LIMPIO NO SE CALIFICA ---------------------------------------------------
# DECISION DEL NEGOCIO (2026-08-24): "si es redireccion no deberia ni calificarse, porque es
# algo que no le compete, y la mayoria ni explica, seria simplemente redireccionar y ya".
# MEDIDO sobre 2.500 sesiones: 13 son redireccion pura (0,5%) y **12 de 13 daban 4 estrellas**
# -- una nota que le pone la misma calificacion al 92% no esta midiendo nada. Los textos son
# plantillas ("Con gusto atendemos tu solicitud en la linea de...").
#
# VUELVE A SER SKIP, PERO NO SE PIERDE. El 2026-08-20 habia dejado de serlo porque el skip
# BORRABA el traspaso del tablero y el negocio lo queria contar. Eso ya no pasa: la tarjeta de
# sin evaluar desglosa por causa y `SKIP_LABEL` tiene `redireccion` desde entonces, asi que
# como skip se sigue contando, con su renglon y clicable para filtrar.
#
# LA EXCEPCION SALE DEL MISMO ARGUMENTO. "No le compete" vale para el traspaso a una linea
# VIVA. Mandar al cliente a una linea CAIDA o sin numero resoluble SI le compete -- el eligio
# a donde mandarlo y el cliente quedo sin a donde escribir. Ese caso (1 de 13) conserva su
# nota de 2 estrellas.

def test_un_traspaso_a_linea_VIVA_es_limpio():
    """La DECISION vive en el orden del worker, no en `evaluate_session`: ahi correria antes
    del chequeo de cortesia, y el negocio decidio el 2026-08-07 que el bucket A se queda en
    `sin_motivo`. Aca se fija la señal; el ruteo lo fija tests/test_worker.py."""
    from src.redireccion import traspaso_limpio

    msgs = [{"from_me": False, "is_note": False, "body": "hola"},
            _op("👋 Con gusto atendemos tu solicitud en la línea de jugadores 0959803754")]
    assert traspaso_limpio(msgs, LINEAS, PROPIA) is True


def test_un_traspaso_SIN_destino_sigue_llevando_nota():
    """Lo unico que en un traspaso es responsabilidad del operador."""
    from src.redireccion import score_redireccion
    from src.redireccion import traspaso_limpio

    msgs = [{"from_me": False, "is_note": False, "body": "hola"},
            _op("escríbeme al siguiente número")]  # traspaso sin numero: queda a la deriva
    assert traspaso_limpio(msgs, LINEAS, PROPIA) is False, (
        "el que deja al cliente sin destino tiene que calificarse")
    r = score_redireccion(msgs, LINEAS, PROPIA)
    assert r is not None and r.stars == 2


def test_un_traspaso_a_linea_CAIDA_sigue_llevando_nota():
    from src.redireccion import traspaso_limpio

    msgs = [{"from_me": False, "is_note": False, "body": "hola"},
            _op("comunicate al siguiente numero 0987013562")]  # DISCONNECTED
    assert traspaso_limpio(msgs, LINEAS, PROPIA) is False


def test_si_ademas_atendio_no_hay_skip():
    """El guard de siempre: si el operador tambien resolvio, la sesion se evalua por su
    motivo real y el traspaso es un detalle."""
    from src.redireccion import traspaso_limpio

    msgs = [{"from_me": False, "is_note": False, "body": "no puedo recargar"},
            _op("ya te acredité el saldo"),
            _op("para próximas, escribí a 0959803754")]
    assert traspaso_limpio(msgs, LINEAS, PROPIA) is False


def test_sin_mapa_de_lineas_no_se_saltea_nada():
    """Falla del lado seguro: sin poder probar que el destino esta vivo, se evalua."""
    from src.redireccion import traspaso_limpio

    msgs = [{"from_me": False, "is_note": False, "body": "hola"},
            _op("escribí a 0959803754")]
    assert traspaso_limpio(msgs, None, None) is False
