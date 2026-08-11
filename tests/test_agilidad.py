"""Tests de src/agilidad.py: rubrica DETERMINISTA de agilidad para el segmento agente.

Todo PURO, en memoria, sin LLM y sin BD. La rubrica mide UNA cosa: el agente pide, el
operador cumple; si es rapido es excelente, si se demora baja, si lo abandona es 1*.

Los tres CONFOUNDS medidos en whaticket_copia y que estos tests fijan como contrato:
  1. horario     — la operacion corre 06:00-23:59 Ecuador; de madrugada no hay nadie
  2. cortesia    — un "gracias"/"ok" no exige respuesta, no puede bajar la nota
  3. ya cerrada  — un comprobante extra posterior a la confirmacion no es abandono
"""
from datetime import datetime, timedelta, timezone

from src.agilidad import (
    MODELO_DETERMINISTA,
    Agilidad,
    bloques_del_cliente,
    calificar_agilidad,
    es_pedido,
    score_agilidad,
    turnos_de_agilidad,
)

# 15:00 Ecuador (UTC-5) = 20:00 UTC. Dentro del horario de operacion.
BASE = datetime(2026, 3, 10, 20, 0, 0, tzinfo=timezone.utc)
# 02:00 Ecuador = 07:00 UTC. Fuera del horario (madrugada).
MADRUGADA = datetime(2026, 3, 10, 7, 0, 0, tzinfo=timezone.utc)


# media_type='chat' es el DEFAULT a proposito: es lo que la BD guarda para un mensaje
# de texto de WhatsApp (679.081 filas en whaticket_copia). El default anterior era None,
# una forma que la BD no produce nunca, y por eso el suite entero podia estar verde con
# `es_pedido` roto en produccion. Si un fixture no tiene la forma del dato real, no
# prueba nada. Para un adjunto de verdad hay que pasar media="image" explicito.
def _cli(minutos, body="Me ayuda con una recarga", media="chat", base=BASE):
    return {"created_at": base + timedelta(minutes=minutos), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(minutos, body="Tu saldo ya esta disponible", base=BASE, media="chat"):
    return {"created_at": base + timedelta(minutes=minutos), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR",
            "media_type": media}


def _nota(minutos, base=BASE):
    return {"created_at": base + timedelta(minutes=minutos), "from_me": True,
            "is_note": True, "body": "nota interna"}


# --- es_pedido: el bloque del agente exige respuesta? -----------------------------

def test_texto_que_pide_algo_es_pedido():
    assert es_pedido([_cli(0, "Me ayuda con una recarga a mi agencia")]) is True


def test_cortesia_sola_no_es_pedido():
    for texto in ("Gracias", "ok", "Dale", "Listo", "Perfecto", "ya esta", "Bendiciones"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_saludo_solo_no_es_pedido():
    for texto in ("Hola", "Buenos dias", "Buenas tardes", "Buenas noches"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_comprobante_SIEMPRE_es_pedido_aunque_el_texto_sea_cortesia():
    # Medido en la copia: el 98,6% de los turnos sin respuesta llevan comprobante, y
    # muchos vienen con un "gracias" al lado. La imagen exige confirmacion: manda la media.
    bloque = [_cli(0, "Gracias"), _cli(0, None, media="image/jpeg")]
    assert es_pedido(bloque) is True


def test_bloque_vacio_sin_media_no_es_pedido():
    assert es_pedido([_cli(0, "")]) is False
    assert es_pedido([_cli(0, None)]) is False


def test_cortesia_seguida_de_un_pedido_en_el_mismo_bloque_es_pedido():
    # El bloque se juzga COMPLETO: "gracias, me ayudas con otra recarga" pide algo.
    assert es_pedido([_cli(0, "Gracias"), _cli(1, "me ayudas con otra recarga")]) is True


# --- es_pedido con la FORMA REAL de la BD -------------------------------------------
# Un mensaje de texto de WhatsApp NO llega con media_type vacio: llega con 'chat'
# (679.081 filas en whaticket_copia). Los tests de arriba usaban media=None, una forma
# que la BD no produce, y por eso no veian que el chequeo de media capturaba TODO
# mensaje no vacio y dejaba la regla de cortesia como codigo muerto. Consecuencia
# medida en produccion: 2 de los 4 veredictos de 1 estrella eran falsos (el "pedido sin
# responder" era la palabra "Gracias" y la palabra "Ok").

def test_cortesia_con_media_type_chat_NO_es_pedido():
    # 'chat' es el media_type de un texto normal, no un adjunto. No puede exigir respuesta.
    for texto in ("Gracias", "Ok", "Listo", "Ya esta", "Buenos dias"):
        assert es_pedido([_cli(0, texto, media="chat")]) is False, texto


def test_texto_que_pide_algo_con_media_type_chat_SI_es_pedido():
    assert es_pedido([_cli(0, "Me ayuda con una recarga", media="chat")]) is True


def test_cortesia_COMPUESTA_no_es_pedido():
    # El agente casi nunca cierra con UNA palabra. Strings reales de whaticket_copia
    # que la regla vieja contaba como pedido sin responder porque solo sabia matchear
    # UN token y despues exigia fin de string.
    for texto in ("hola buenos dias", "hola buenas noches", "ok muy bien",
                  "listo gracias", "si gracias", "no muchas gracias",
                  "ok esta bien", "esta bien", "muy amable"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_cortesia_con_vocabulario_faltante_no_es_pedido():
    # Tokens reales del dataset que no estaban en el vocabulario.
    for texto in ("tks", "thanks", "bueno", "buen dia"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_cortesia_con_emojis_no_es_pedido():
    # Emojis reales encontrados: la clase de caracteres vieja no los cubria.
    for texto in ("muchas gracias ☺️", "gracias 🫂", "muchas gracias 🍀🤝🏻", "Listo!!"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_una_sola_palabra_de_verdad_lo_devuelve_a_pedido():
    # El vocabulario es conservador POR DISEÑO: alcanza una palabra que pida algo para
    # que el bloque vuelva a exigir respuesta. Strings reales de la misma tanda.
    for texto in ("comision", "pago de comision", "ganancia de jugador",
                  "no puede ingresar", "esto por favor", "por fa",
                  "gracias me avisa", "ok y el pago cuando sale"):
        assert es_pedido([_cli(0, texto)]) is True, texto


def test_el_comprobante_DEL_OPERADOR_cuenta_como_confirmacion():
    # Caso real 5177aa96: el agente pide un retiro, el operador responde 25 min despues
    # con el COMPROBANTE (imagen, sin una sola palabra de confirmacion) y el agente manda
    # una imagen final que nadie contesta. La regla lo llamaba abandono y ponia 1 estrella.
    # El operador cumplio: la nota tiene que ser por LENTITUD (2), no por abandono (1).
    msgs = [
        _cli(0, "Un retiro 60, cedula 1312282153"),
        _op(25, body="", media="image"),
        _cli(31, None, media="image"),
    ]
    a = calificar_agilidad(msgs)
    assert a.stars == 2, a.rationale
    assert a.sin_respuesta == 0


def test_sin_comprobante_ni_confirmacion_el_abandono_SIGUE_siendo_1():
    # El contrafactual: si el operador nunca respondio nada, el piso de 1 estrella se
    # mantiene. El fix de arriba no puede desactivar el castigo real.
    msgs = [_cli(0, "Un retiro 60, cedula 1312282153")]
    a = calificar_agilidad(msgs)
    assert a.stars == 1
    assert a.sin_respuesta == 1


def test_solo_la_media_REAL_fuerza_el_pedido():
    # Adjuntos de verdad: exigen confirmacion aunque el texto sea cortesia.
    for tipo in ("image", "image/jpeg", "video", "audio", "document", "sticker"):
        assert es_pedido([_cli(0, "Gracias", media=tipo)]) is True, tipo
    # Tipos que NO son un adjunto del agente: no fuerzan nada (misma lista que
    # src/signals.py, que ya excluia 'chat'/'missed'/'template'/'location').
    for tipo in ("chat", "missed", "template", "location"):
        assert es_pedido([_cli(0, "Gracias", media=tipo)]) is False, tipo


# --- bloques y turnos --------------------------------------------------------------

def test_bloques_agrupan_mensajes_consecutivos_del_cliente():
    msgs = [_cli(0, "Buenas"), _cli(0, "Una recarga"), _op(1), _cli(5, "Otra mas"), _op(6)]
    bloques = bloques_del_cliente(msgs)
    assert [len(b) for b in bloques] == [2, 1]


def test_las_notas_internas_no_cuentan_como_respuesta():
    # Una nota interna del operador NO es una respuesta al agente.
    msgs = [_cli(0, "Una recarga"), _nota(1), _op(10)]
    t = turnos_de_agilidad(msgs)
    assert len(t) == 1
    assert t[0].espera == timedelta(minutes=10)


def test_turno_sin_respuesta_deja_espera_nula():
    t = turnos_de_agilidad([_cli(0, "Una recarga")])
    assert len(t) == 1
    assert t[0].respuesta_at is None
    assert t[0].espera is None


def test_turno_toma_la_PRIMERA_respuesta_del_operador():
    t = turnos_de_agilidad([_cli(0, "Una recarga"), _op(3), _op(9)])
    assert t[0].espera == timedelta(minutes=3)


def test_la_espera_se_mide_desde_el_PRIMER_mensaje_del_bloque():
    # Es la espera que PERCIBE el agente: desde que empezo a pedir.
    t = turnos_de_agilidad([_cli(0, "Buenas"), _cli(2, "Una recarga"), _op(4)])
    assert t[0].espera == timedelta(minutes=4)


# --- horario de operacion ----------------------------------------------------------

def test_pedido_en_horario_esta_dentro():
    t = turnos_de_agilidad([_cli(0, "Una recarga"), _op(1)])
    assert t[0].en_horario is True


def test_pedido_de_madrugada_queda_FUERA_de_horario():
    # 02:00 Ecuador: no hay nadie. La espera hasta que entra el turno no es lentitud.
    t = turnos_de_agilidad([_cli(0, "Una recarga", base=MADRUGADA),
                            _op(240, base=MADRUGADA)])
    assert t[0].en_horario is False


def test_la_madrugada_no_baja_la_nota():
    msgs = [_cli(0, "Una recarga", base=MADRUGADA), _op(300, base=MADRUGADA)]
    a = calificar_agilidad(msgs)
    assert a.stars is None, "sin pedidos en horario no hay nota de agilidad"
    assert a.turnos_pedido == 0


# --- bandas --------------------------------------------------------------------

def test_hasta_2_minutos_es_excelente():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(2)])
    assert (a.stars, a.label) == (5, "excelente")


def test_entre_2_y_5_minutos_es_buena():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(4)])
    assert (a.stars, a.label) == (4, "buena")


def test_entre_5_y_15_minutos_es_aceptable():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(10)])
    assert (a.stars, a.label) == (3, "aceptable")


def test_mas_de_15_minutos_es_deficiente():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(40)])
    assert (a.stars, a.label) == (2, "deficiente")


def test_los_bordes_son_inclusivos_hacia_la_banda_mejor():
    assert calificar_agilidad([_cli(0, "Recarga"), _op(2)]).stars == 5    # 2min exactos
    assert calificar_agilidad([_cli(0, "Recarga"), _op(5)]).stars == 4    # 5min exactos
    assert calificar_agilidad([_cli(0, "Recarga"), _op(15)]).stars == 3   # 15min exactos


# --- regla del PEOR turno ("debe ser SIEMPRE rapido") -----------------------------

def test_manda_el_PEOR_pedido_no_el_promedio():
    # Dos pedidos: uno en 1 min y otro en 20. La sesion NO es excelente.
    msgs = [_cli(0, "Una recarga"), _op(1),
            _cli(30, "Otra recarga"), _op(50)]
    a = calificar_agilidad(msgs)
    assert (a.stars, a.label) == (2, "deficiente")
    assert a.peor_espera == timedelta(minutes=20)


def test_una_cortesia_lenta_NO_baja_la_nota():
    # El agente agradece y el operador no corre a contestar: no es una falla.
    msgs = [_cli(0, "Una recarga"), _op(1), _cli(5, "Gracias"), _op(60)]
    a = calificar_agilidad(msgs)
    assert (a.stars, a.label) == (5, "excelente")
    assert a.turnos_pedido == 1


# --- 1 estrella: abandono ---------------------------------------------------------

def test_pedido_sin_respuesta_y_sin_confirmacion_previa_es_mala():
    a = calificar_agilidad([_cli(0, "Una recarga")])
    assert (a.stars, a.label) == (1, "mala")
    assert a.sin_respuesta == 1


def test_comprobante_sin_respuesta_es_mala():
    a = calificar_agilidad([_cli(0, None, media="image/jpeg")])
    assert (a.stars, a.label) == (1, "mala")


def test_comprobante_extra_DESPUES_de_confirmar_NO_es_abandono():
    # Medido: 1.654 de 3.283 comprobantes sin respuesta ya tenian confirmacion previa
    # ("saldo ya esta disponible"). La operacion estaba cerrada: no es abandono.
    msgs = [_cli(0, None, media="image/jpeg"),
            _op(1, "Tu saldo ya esta disponible"),
            _cli(3, None, media="image/jpeg")]
    a = calificar_agilidad(msgs)
    assert a.label != "mala"
    assert a.stars == 5, "el unico pedido atendido fue en 1 min"


def test_la_cortesia_final_sin_respuesta_NO_es_abandono():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(1), _cli(5, "Gracias")])
    assert (a.stars, a.label) == (5, "excelente")
    assert a.sin_respuesta == 0


def test_el_abandono_gana_sobre_una_espera_buena():
    # Un pedido atendido rapido no compensa otro abandonado sin confirmar nada.
    msgs = [_cli(0, "Una recarga"), _op(1, "ya veo"), _cli(20, "Y la otra recarga?")]
    a = calificar_agilidad(msgs)
    assert (a.stars, a.label) == (1, "mala")


# --- sin material para juzgar ----------------------------------------------------

def test_sesion_sin_pedidos_no_tiene_nota():
    a = calificar_agilidad([_cli(0, "Gracias"), _op(1)])
    assert a.stars is None
    assert a.label is None


def test_sesion_vacia_no_revienta():
    a = calificar_agilidad([])
    assert isinstance(a, Agilidad)
    assert a.stars is None


def test_el_rationale_explica_la_nota_con_el_numero():
    a = calificar_agilidad([_cli(0, "Una recarga"), _op(4)])
    assert "4" in a.rationale or "240" in a.rationale
    assert a.rationale


# --- adaptador a ScoreResult (lo que consume build_score_record) -------------------

def test_score_agilidad_devuelve_un_ScoreResult_sin_llamar_al_LLM():
    s = score_agilidad([_cli(0, "Una recarga"), _op(1)])
    assert s is not None
    assert (s.stars, s.rating_label) == (5, "excelente")
    # Sentinela auditable: permite saber por SQL que filas salieron del path determinista.
    assert s.llm_model == MODELO_DETERMINISTA
    assert s.rubric == "agilidad"


def test_score_agilidad_no_usa_los_ejes_del_jugador():
    # `atencion` (empujo|pasivo) es la vara COMERCIAL del jugador: no aplica a un
    # revendedor. deposit_observed es la observacion del LLM, y aca no hay LLM.
    s = score_agilidad([_cli(0, "Una recarga"), _op(1)])
    assert s.atencion is None
    assert s.deposit_observed is None
    assert s.motivo is None


def test_score_agilidad_expone_los_numeros_en_dimensions():
    s = score_agilidad([_cli(0, "Una recarga"), _op(4)])
    assert s.dimensions["peor_espera_seg"] == 240
    assert s.dimensions["turnos_pedido"] == 1
    assert s.dimensions["sin_respuesta"] == 0


def test_score_agilidad_recomienda_cuando_se_demora():
    s = score_agilidad([_cli(0, "Una recarga"), _op(40)])
    assert s.stars == 2
    assert s.recomendacion, "una nota baja tiene que decir que hacer distinto"


def test_score_agilidad_no_recomienda_cuando_es_excelente():
    s = score_agilidad([_cli(0, "Una recarga"), _op(1)])
    assert s.recomendacion == ""


def test_score_agilidad_devuelve_None_si_no_hay_nada_que_medir():
    # Sin pedidos en horario no hay nota: el caller decide (no se inventa un 3).
    assert score_agilidad([_cli(0, "Gracias"), _op(1)]) is None
    assert score_agilidad([]) is None


# --- LO QUE NO PIDE NADA Y SE LEIA COMO PEDIDO (auditoria del 2026-08-11) ---------

def test_un_mensaje_de_PURO_EMOJI_no_es_pedido():
    # BUG: `es_cortesia` normaliza sacando todo lo que no es palabra, asi que un mensaje
    # de puro emoji deja la lista VACIA, y `bool([]) and all(...)` da False -> al revés de
    # la intencion. `es_pedido` lo tomaba como pedido real y la sesion sacaba 1 estrella
    # por un cierre agradecido que nadie tenia que contestar.
    # Casos reales: 6315c196 cierra con "🙌🏻" y 822f5cb4 con "🫱🏼‍🫲🏼", ambos 1★ falso.
    for emoji in ("🙌🏻", "🫱🏼‍🫲🏼", "👍", "🍀", "❤️", "...", "!!!"):
        assert es_pedido([_cli(0, emoji)]) is False, emoji


def test_el_texto_VACIO_sigue_sin_ser_pedido():
    # No-regresion del criterio de `es_cortesia`: vacio no es cortesia, es ausencia de
    # texto, y ahi decide `es_pedido` (un bloque vacio sin adjunto no pide nada).
    assert es_pedido([_cli(0, "")]) is False
    assert es_pedido([_cli(0, "   ")]) is False


def test_el_saludo_AUTOMATICO_del_widget_web_no_es_pedido():
    # Lo genera el widget de la web, no lo escribe la persona. 2.385 sesiones (4,5% del
    # segmento agente) arrancan asi y el 100% NO tiene ningun media real en toda la
    # sesion: nunca hay comprobante ni transaccion. Se median igual como pedido de caja.
    # Caso 3e5a48f2: todo el chat es esa frase, el operador saluda 5min7s despues, y esos
    # 7 segundos sobre el umbral bajaban la sesion entera a 3 estrellas.
    for texto in ("Hola, estoy escribiendo desde sorti.ec",
                  "Hola, te escribo desde sorti.ec",
                  "Hola 👋 estoy escribiendo desde sorti.ec"):
        assert es_pedido([_cli(0, texto)]) is False, texto


def test_el_widget_con_un_pedido_pegado_SI_es_pedido():
    # El guard es para el opener SOLO. Si la persona escribio algo mas, es un pedido.
    assert es_pedido([_cli(0, "Hola, estoy escribiendo desde sorti.ec, necesito una recarga")]) is True


# --- UN HUECO GRANDE DENTRO DEL BLOQUE NO ES UNA RAFAGA ---------------------------
# `bloques_del_cliente` cortaba SOLO cuando hablaba el operador, sin limite de tiempo. Y
# `turnos_de_agilidad` es asimetrico a proposito: el reloj arranca en el PRIMER mensaje del
# bloque y la respuesta se busca despues del ULTIMO. Combinados, cualquier silencio del
# cliente adentro del bloque se le cobraba al operador.
#
# CASO REAL `ec562888-6d8f-4b25-bf92-6e1b61cf4d0f` (Italo Santibañez, 10-ago): el cliente dice
# "Gracias" 21:40 y recien 23:25 manda el formulario de retiro. Sin operador en medio, las dos
# cosas eran UN bloque: reloj desde el "Gracias", respuesta despues del formulario -> 6.336
# segundos = 1,76 h, y la sesion cayo a 2 estrellas. El operador contesto el pedido real en
# segundos; lo que se le cobro fue lo que el cliente tardo en pedir.
#
# EL UMBRAL SALE DE LOS DATOS, no de la intuicion: sobre 3.102 huecos intra-bloque del
# segmento agente, el 96,2% es de 5 minutos o menos (p95 = 215s) y hay 46 de mas de una hora.
# 15 minutos es 3x el p95, asi que corta solo lo que evidentemente no es tipeo seguido: 68
# huecos de 3.102 (2,2%). La duda favorece al operador.

def test_una_rafaga_sigue_siendo_UN_bloque():
    msgs = [_cli(0, "hola"), _cli(0.5, "necesito una recarga"), _cli(1, "de 50"),
            _op(2, "dale, ya te la cargo")]
    assert len(bloques_del_cliente(msgs)) == 1


def test_un_hueco_de_mas_de_15_minutos_ABRE_un_bloque_nuevo():
    msgs = [_cli(0, "Gracias"), _cli(20, "Me ayudas con una recarga"),
            _op(21, "dale")]
    bloques = bloques_del_cliente(msgs)
    assert len(bloques) == 2, bloques
    assert bloques[0][0]["body"] == "Gracias"


def test_el_umbral_es_15_minutos():
    # 14 minutos sigue siendo la misma rafaga; 16 ya no.
    assert len(bloques_del_cliente([_cli(0, "hola"), _cli(14, "una recarga")])) == 1
    assert len(bloques_del_cliente([_cli(0, "hola"), _cli(16, "una recarga")])) == 2


def test_el_caso_de_italo_el_gracias_ya_no_ancla_el_reloj():
    # "Gracias" a los 0 min, el pedido real 105 min despues, y el operador contesta en 1 min.
    msgs = [_cli(0, "Gracias"),
            _cli(105, "*Nombre de Agencia* SELLAN *Monto:* $120,00"),
            _op(106, "listo, procesado")]
    turnos = turnos_de_agilidad(msgs)
    pedidos = [t for t in turnos if t.es_pedido]
    assert len(pedidos) == 1, f"el 'Gracias' no tiene que contar como pedido: {turnos}"
    assert pedidos[0].espera is not None
    assert pedidos[0].espera.total_seconds() <= 120, \
        f"la espera tiene que ser del pedido real, no del 'Gracias': {pedidos[0].espera}"
    r = calificar_agilidad(msgs)
    assert r.stars == 5, f"{r.stars}★ {r.rationale}"
