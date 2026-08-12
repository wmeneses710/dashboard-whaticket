"""Tests de src/deposito.py: rubrica del motivo `deposito`, 100% DETERMINISTA.

Todo PURO, en memoria, sin LLM y sin BD. El motivo lo sigue clasificando el modelo;
lo que se determina aca es la NOTA, porque los tres hechos que la definen son
verificables: el reloj, si confirmo la acreditacion, y si chequeo que no faltara nada.

ESCALA (definida por el negocio el 2026-08-06; para deposito "con que se haga bien y
rapido es suficiente", y el comprobante se exige por AUDITORIA y proteccion de la
confianza, no como metrica de satisfaccion):
    5  acuse <=2 min + confirmo la acreditacion + se aseguro de que no faltara nada
    4  acuse <=2 min + confirmo la acreditacion
    3  confirmo, pero el acuse tardo 2-5 min
    2  el acuse tardo >5 min, o nunca confirmo la acreditacion
    1  ni respondio ni confirmo

Umbrales calibrados sobre 1.254 transacciones (1 por persona, jul-ago 2026):
el 78,0% acusa en <=2 min y el 76,2% confirma en <=5 min del comprobante.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.deposito import calificar_deposito, score_deposito

BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)


def _cli(minutos, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _comprobante(minutos):
    return _cli(minutos, body="", media="image")


def _op(minutos, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


ACUSE = "Estamos verificando tu comprobante. Tu recarga se reflejara en breve."
ACREDITA = "Gracias por tu recarga. Tu saldo ya esta disponible."
ALGO_MAS = "¿Hay algo mas en lo que te pueda ayudar?"


# --- el corte transaccion / consulta ----------------------------------------

def test_sin_comprobante_del_cliente_NO_es_una_transaccion():
    # 64,6% de las sesiones con contexto de recarga son CONSULTAS: preguntan por la
    # recarga sin hacer ninguna. No hay nada que acreditar -> esta rubrica no aplica
    # y devuelve None para que decida el caller.
    msgs = [_cli(0, "como hago para recargar?"), _op(1, "por transferencia bancaria")]
    assert calificar_deposito(msgs) is None
    assert score_deposito(msgs) is None


def test_el_comprobante_del_cliente_activa_la_rubrica():
    msgs = [_cli(0, "les mando el comprobante de la recarga"), _comprobante(0),
            _op(1, ACUSE), _op(3, ACREDITA)]
    assert calificar_deposito(msgs) is not None


# --- la escala ---------------------------------------------------------------

def test_5_estrellas_rapido_confirmado_y_chequeo_que_no_faltara_nada():
    msgs = [_cli(0, "recarga"), _comprobante(0),
            _op(1, ACUSE), _op(3, ACREDITA), _op(4, ALGO_MAS)]
    a = calificar_deposito(msgs)
    assert a.stars == 5 and a.label == "excelente"


def test_4_estrellas_rapido_y_confirmado_pero_cerro_sin_preguntar():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 4 and a.label == "buena"


def test_3_estrellas_confirmo_pero_el_acuse_tardo_entre_2_y_5():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(4, ACUSE), _op(6, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 3 and a.label == "aceptable"


def test_2_estrellas_el_acuse_tardo_mas_de_5_aunque_haya_confirmado():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(9, ACUSE), _op(11, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_2_estrellas_respondio_rapido_pero_NUNCA_confirmo_la_acreditacion():
    # El caso que el detector viejo dejaba pasar: "en breve" y desaparece.
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE)]
    a = calificar_deposito(msgs)
    assert a.stars == 2 and a.label == "deficiente"


def test_1_estrella_no_respondio_nada():
    msgs = [_cli(0, "recarga"), _comprobante(0)]
    a = calificar_deposito(msgs)
    assert a.stars == 1 and a.label == "mala"


# --- lo que NO puede pasar ---------------------------------------------------

def test_la_cortesia_sola_NO_alcanza_para_el_5():
    # El bug que destapo sacar el cap: el 37,1% de los 5 estrellas se ganaban SOLO
    # por ser amables. Ser amable no es lograr el mejor escenario del motivo.
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA),
            _op(4, "Un placer atenderte 😊✨ Gracias por preferirnos! 🍀💚")]
    a = calificar_deposito(msgs)
    assert a.stars == 4, "la despedida cordial no puede valer un 5"


def test_el_bot_no_cuenta_como_respuesta_del_operador():
    bot = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
           "is_note": False, "body": ACUSE, "sent_from": "CHATBOT",
           "media_type": "chat"}
    msgs = [_cli(0, "recarga"), _comprobante(0), bot]
    a = calificar_deposito(msgs)
    assert a.stars == 1


def test_las_notas_internas_no_cuentan():
    nota = {"created_at": BASE + timedelta(minutes=1), "from_me": True,
            "is_note": True, "body": ACREDITA, "media_type": "chat"}
    msgs = [_cli(0, "recarga"), _comprobante(0), nota]
    assert calificar_deposito(msgs).stars == 1


def test_el_reloj_arranca_en_el_COMPROBANTE_no_en_el_primer_mensaje():
    # El cliente saluda, charla, y 30 min despues manda el comprobante. El operador
    # responde 1 min despues de ESO: es un 4, no se le imputa la charla previa.
    msgs = [_cli(0, "buenas, queria hacer una recarga"), _op(1, "buenas, dale"),
            _comprobante(30), _op(31, ACUSE), _op(32, ACREDITA)]
    a = calificar_deposito(msgs)
    assert a.stars == 4


def test_sin_created_at_no_revienta_y_cede_el_turno():
    # `fetch_messages` (path por conversacion) NO trae created_at; solo lo trae
    # `fetch_session_messages`. Es la misma trampa documentada en src/context.py, que
    # ya habia reventado la rubrica de agilidad contra la BD. Sin reloj no hay nota que
    # dar: se devuelve None y decide el caller, en vez de explotar con KeyError.
    msgs = [{"from_me": False, "is_note": False, "body": "les mando la recarga",
             "media_type": "chat"},
            {"from_me": False, "is_note": False, "body": "", "media_type": "image"},
            {"from_me": True, "is_note": False, "body": ACREDITA, "media_type": "chat"}]
    assert calificar_deposito(msgs) is None
    assert score_deposito(msgs) is None


def test_score_deposito_devuelve_un_ScoreResult_usable():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA),
            _op(4, ALGO_MAS)]
    r = score_deposito(msgs)
    assert r.motivo == "deposito"
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == "", "en el mejor escenario no hay nada que recomendar"


def test_la_recomendacion_dice_QUE_falto_para_el_5():
    msgs = [_cli(0, "recarga"), _comprobante(0), _op(1, ACUSE), _op(3, ACREDITA)]
    r = score_deposito(msgs)
    assert r.stars == 4
    assert "algo mas" in r.recomendacion.lower() or "algo más" in r.recomendacion.lower()


# --- LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL COMPROBANTE, NO EN TODA LA SESION ---
# Caso `f9b31f4f-6399-4e76-96ce-3a1b726aa7da`: 84 mensajes, 8 dias, 16 cierres del operador
# y cuatro operadores distintos, todo en UNA conversacion porque el CRM no abrio filas
# nuevas. Un comprobante del 3-ago que NADIE contesto se emparejaba con el saludo y la
# acreditacion de otra transaccion del 6-ago, y salia "Confirmo la acreditacion, pero tardo
# 39,5 horas en avisarle" -- un hecho que no ocurrio.
# La frontera es la nota de cierre del operador (ver src/interacciones.py).

def _cierre(minutos, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def test_la_acreditacion_de_OTRA_interaccion_no_cuenta():
    # FIXTURE INVERTIDO el 2026-08-12 al pasar el ancla a la ULTIMA visita. El invariante que
    # este test protege es el mismo -- la evidencia NO se cruza entre interacciones -- pero
    # ahora la ventana juzgada es la ultima, asi que la acreditacion que no debe filtrarse es
    # la ANTERIOR. Antes el fixture probaba la direccion opuesta y por eso exigia 1★ sobre la
    # primera visita: esa expectativa quedo obsoleta, el invariante no.
    # Primera visita: recarga acreditada. Dias despues: comprobante que nadie contesta.
    msgs = [_cli(0, "les mando el comprobante de la recarga"), _comprobante(0),
            _op(1, "Gracias por tu recarga, tu saldo ya esta disponible"), _cierre(5),
            _cli(2880, "otra recarga"), _comprobante(2880), _cierre(2882)]
    d = calificar_deposito(msgs)
    assert d is not None
    assert d.acredito is False, "la acreditacion del 1er dia no puede acreditar el 2do comprobante"
    assert d.stars == 1, f"{d.stars}★ {d.rationale}"
    assert "39" not in d.rationale and "hora" not in d.rationale.lower()


def test_sin_cierres_la_ventana_sigue_siendo_toda_la_sesion():
    # No-regresion del 96,3% de las conversaciones: un solo cierre (o ninguno) = una sola
    # interaccion, y todo funciona como antes.
    msgs = [_cli(0, "les mando el comprobante de la recarga"), _comprobante(0),
            _op(1, "Gracias por tu recarga, tu saldo ya esta disponible")]
    d = calificar_deposito(msgs)
    assert d is not None and d.acredito is True and d.stars == 4


# --- LA RAMA DEL RECHAZO ------------------------------------------------------------
# HALLADA leyendo los 2 estrellas de produccion el 2026-08-12. La rubrica no tenia rama para
# "el deposito NO se podia acreditar por una razon VALIDA", y trataba la ausencia de
# confirmacion como falla del operador SIEMPRE. Dos casos reales de 5:
#   `0b3389f6`: el comprobante fue RECHAZADO. Anggie contesto en 14 s "Titular incorrecto" y
#               el cliente dijo "Si ya me di cuenta". La plata nunca entro porque la boleta
#               era invalida, y la nota la castigaba por "nunca confirmo".
#   `b2369395`: el usuario no estaba verificado. Arturo dijo "para realizar cargas debe
#               verificar su usuario" y mando un video-tutorial. Hizo lo correcto -> 2 estrellas.
#
# LA DECISION (2026-08-12): cuando la plata no puede entrar, el trabajo del operador es
# DECIRLO, rapido y claro. Se califica por la velocidad de ese aviso, con TECHO EN 4:
#   4  informo el rechazo dentro de los 2 min
#   3  lo informo, pero tarde
#   2  nunca le dijo nada (el cliente queda sin saber por que no le entro)
# El 5 NO es alcanzable en esta rama a proposito: significa "el mejor escenario del motivo",
# y un deposito rechazado no lo es. No es un castigo -- el techo es honesto y mantiene el
# incentivo de ayudar al cliente a arreglarlo para que el proximo intento si entre.
#
# DOS FALSOS POSITIVOS que hay que evitar, medidos en la base:
#   "monto minimo 5"  -> 20.489 mensajes: es la PLANTILLA de como transferir, no un rechazo.
#   "El bono esta vigente" -> "vigente" en contexto POSITIVO.
# El contexto desambigua el resto: la rama solo mira mensajes POSTERIORES al comprobante en
# sesiones donde NO hubo acreditacion, asi que "debe verificar" ahi si es el motivo del rechazo.

def test_rechazo_informado_rapido_es_4():
    r = calificar_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(0, "", media="image"),
        _op(1, "Titular incorrecto, la cuenta debe estar a tu nombre"),
    ], _cierre(10))
    assert r.stars == 4, r.rationale
    assert "no se pudo acreditar" in r.rationale or "rechaz" in r.rationale.lower(), r.rationale


def test_rechazo_informado_TARDE_es_3():
    r = calificar_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(0, "", media="image"),
        _op(8, "Boleta repetida"),   # la frase real de la base, sin "cargada"
    ], _cierre(20))
    assert r.stars == 3, r.rationale


def test_sin_verificar_es_un_rechazo_valido():
    # El caso `b2369395`: no se puede cargar porque el usuario no esta verificado.
    r = calificar_deposito([
        _cli(0, "sera posible que me ayude con una recarga"), _cli(0, "", media="image"),
        _op(1, "para realizar cargas y retiros debe verificar su usuario"),
        _op(2, "Aquí le dejo un video de como hacerlo", media="video"),
    ], _cierre(30))
    assert r.stars == 4, r.rationale


def test_la_PLANTILLA_del_monto_minimo_no_es_un_rechazo():
    # 20.489 mensajes la tienen: es la instruccion de como transferir. Si contara como
    # rechazo, cualquier deposito sin acreditar pasaria de 2 a 4.
    r = calificar_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(0, "", media="image"),
        _op(1, "Deja el concepto en blanco. Monto mínimo: $5. Gracias por tomarlo en cuenta"),
    ], _cierre(10))
    assert r.stars == 2, r.rationale


def test_un_deposito_que_SI_se_acredito_no_entra_a_la_rama():
    # Guard: la rama es solo para cuando NO hubo acreditacion.
    r = calificar_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(0, "", media="image"),
        _op(1, "recibido"), _op(2, "listo, tu saldo ya está disponible"),
    ], _cierre(10))
    assert r.stars == 4 and r.acredito is True, r.rationale


def test_el_rechazo_ANTES_del_comprobante_no_cuenta():
    # Si el operador dijo "debe verificar" ANTES de que llegue el comprobante, no es el
    # motivo del rechazo de ESTE comprobante.
    r = calificar_deposito([
        _cli(0, "quiero recargar"),
        _op(1, "para realizar cargas debe verificar su usuario"),
        _cli(5, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(6, "ahi lo reviso"),
    ], _cierre(20))
    assert r.stars == 2, r.rationale


# --- SE JUZGA LA ULTIMA VISITA, NO LA PRIMERA ---------------------------------------
# El ancla tomaba el PRIMER comprobante de la sesion, o sea la visita MAS VIEJA. Y una sesion
# mergea todos los episodios del ticket: MEDIDO el 2026-08-12 sobre 1.180 sesiones con 2+
# interacciones calificables, la primera y la ultima estan separadas por una mediana de 8,6 h,
# un p90 de 285 h (12 dias) y un maximo de 266 dias.
# El criterio viejo era ademas el SEGUNDO MAS DURO de los seis que se midieron (3,42★ contra
# 3,55★ del ultimo; 620 sesiones en 2★ o menos contra 499).
# Y lo decisivo: **82% de esas sesiones tienen mas de un operador** (hasta 10). Con el primero,
# la nota se le cargaba al que atendio la visita vieja: cambiar a la ultima reatribuye 494 de
# las 600 notas que se mueven. Por eso NO se promedia entre interacciones -- seria mezclar el
# trabajo de dos personas y ponerselo a una.

def _cierre(minutos, quien="Ana"):
    return {"created_at": BASE + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación",
            "media_type": "chat"}


def test_se_juzga_la_ULTIMA_visita_no_la_primera():
    # Visita 1: comprobante que nadie contesto (seria 1★). Visita 2, dos dias despues:
    # comprobante acreditado en un minuto (4★). La nota describe la SEGUNDA.
    msgs = [_comprobante(0), _cierre(30),
            _comprobante(2880), _op(2881, ACREDITA), _cierre(2882)]
    d = calificar_deposito(msgs)
    assert d.stars == 4, d.rationale
    assert d.acredito is True


def test_el_reloj_arranca_en_el_PRIMER_comprobante_de_la_ventana():
    # El ancla elige la INTERACCION (la ultima), pero adentro el reloj arranca en el primer
    # comprobante de ESA visita: si el cliente manda tres imagenes seguidas, la espera se
    # cuenta desde la primera. Tomar la ultima del tramo escondería la demora.
    msgs = [_comprobante(0), _cierre(10),
            _comprobante(100), _comprobante(101), _comprobante(102),
            _op(110, ACREDITA), _cierre(111)]
    d = calificar_deposito(msgs)
    # 10 min desde el primer comprobante de la visita, no 8 desde el ultimo
    assert d.espera is not None and 9.5 <= d.espera.total_seconds() / 60 <= 10.5, d.espera


def test_sin_cierres_sigue_siendo_UNA_sola_interaccion():
    # El 96,3% de las sesiones son una interaccion: ahi primero y ultimo son lo mismo y no
    # puede cambiar nada.
    msgs = [_comprobante(0), _op(1, ACUSE), _op(3, ACREDITA)]
    d = calificar_deposito(msgs)
    assert d.stars == 4, d.rationale
