"""Tests de la deteccion determinista de comprobantes de recarga (depositos).

El "deposito" del analisis NO es monto: es cuantas VECES el cliente manda un
comprobante (imagen) junto con la razon (recarga). Esta capa determinista es el
GATE: acota los falsos positivos (sin comprobante del cliente -> 0), y la
confirmacion final del LLM opera solo dentro de lo elegible.

Mensajes = dicts con: from_me, is_note, body, media_type.
"""
from src.deposits import (
    deposit_candidate_count,
    has_recharge_context,
    receipt_image_count,
)


def _cli_img(body=""):
    return {"from_me": False, "is_note": False, "body": body, "media_type": "image"}


def _cli_txt(body):
    return {"from_me": False, "is_note": False, "body": body, "media_type": "chat"}


def _age_txt(body):
    return {"from_me": True, "is_note": False, "body": body, "media_type": "chat"}


def test_sin_mensajes_es_cero():
    assert deposit_candidate_count([]) == 0


def test_imagen_del_cliente_con_contexto_de_recarga_cuenta():
    msgs = [_cli_txt("hola quiero hacer una recarga"), _cli_img()]
    assert deposit_candidate_count(msgs) == 1


def test_imagen_del_cliente_sin_contexto_no_cuenta_control_fp():
    # Imagen sin ninguna razon de recarga: no es un comprobante -> 0 (evita FP).
    msgs = [_cli_txt("miren esta foto"), _cli_img()]
    assert deposit_candidate_count(msgs) == 0


def test_texto_de_recarga_sin_imagen_no_cuenta():
    # El comprobante ES la imagen; "quiero recargar" sin comprobante no cuenta.
    msgs = [_cli_txt("quiero recargar 500")]
    assert deposit_candidate_count(msgs) == 0


def test_imagen_del_agente_no_cuenta():
    # El comprobante lo manda el CLIENTE, no el agente.
    msgs = [_cli_txt("recarga"), {"from_me": True, "is_note": False, "body": "", "media_type": "image"}]
    assert receipt_image_count(msgs) == 0
    assert deposit_candidate_count(msgs) == 0


def test_imagen_en_nota_interna_se_ignora():
    msgs = [_cli_txt("recarga"), {"from_me": False, "is_note": True, "body": "", "media_type": "image"}]
    assert deposit_candidate_count(msgs) == 0


def test_varias_imagenes_del_cliente_cuenta_las_veces():
    # Dos comprobantes en la misma conversacion = 2 depositos.
    msgs = [_cli_txt("dos recargas"), _cli_img(), _age_txt("dale"), _cli_img()]
    assert deposit_candidate_count(msgs) == 2


def test_media_type_no_imagen_no_cuenta():
    msgs = [
        _cli_txt("recarga"),
        {"from_me": False, "is_note": False, "body": "", "media_type": "audio"},
        {"from_me": False, "is_note": False, "body": "", "media_type": None},
    ]
    assert receipt_image_count(msgs) == 0


def test_el_contexto_de_recarga_lo_pone_el_CLIENTE_no_el_operador():
    # Plantillas REALES de venta del operador. Mencionan la recarga pero el cliente
    # nunca dijo que iba a recargar: no son contexto de recarga, son pitch.
    # Medido el 2026-08-06: leyendo tambien al operador, el gate pasaba de 519 a 885
    # sesiones (41,4% inflado), y las sesiones asi eran `promo`/`registro`
    # ("¿Como reclamo mis 10 giros?"), no depositos.
    for pitch in ("Registrate, verifica tu cuenta y con tu primera carga comienza a "
                  "disfrutar de todos los beneficios",
                  "Hola, depositas 5 amiga y recibes 5 mas",
                  "Le comento que las cargas y los retiros se pueden realizar por "
                  "medio de transferencias bancarias"):
        assert has_recharge_context([_age_txt(pitch)]) is False, pitch


def test_una_imagen_del_cliente_con_pitch_del_operador_NO_es_deposito():
    # El caso exacto que inflaba el gate: el operador habla de recargar y el cliente
    # manda cualquier imagen. Sin que el CLIENTE mencione la recarga, no hay deposito.
    msgs = [_age_txt("con tu primera carga activas la promo"), _cli_img()]
    assert deposit_candidate_count(msgs) == 0


def test_has_recharge_context_detecta_variantes():
    for kw in ["recarga", "una RECARGA", "comprobante adjunto", "el deposito", "depósito", "transferencia"]:
        assert has_recharge_context([_cli_txt(kw)]) is True
    assert has_recharge_context([_cli_txt("hola buenas tardes")]) is False


def test_abono_a_deuda_es_contexto_de_recarga():
    # Flujo de altisimo volumen: "Abono N a deuda" + comprobante = recarga.
    assert has_recharge_context([_cli_txt("Abono 10 a deuda")]) is True
    msgs = [_cli_img(), _cli_txt("Abono 10 a deuda")]
    assert deposit_candidate_count(msgs) == 1


def test_receipt_image_cuenta_solo_imagenes_del_cliente():
    msgs = [_cli_img(), _cli_img(), _age_txt("hola"), _cli_txt("gracias")]
    assert receipt_image_count(msgs) == 2


# --- vocabulario REAL del cliente (auditoria del 2026-08-11) -------------------

def test_vocabulario_de_recarga_que_usa_el_cliente_de_verdad():
    # El cliente de este negocio no dice "recargar": dice "cargar", "recargueme" o
    # "acreditando". Medido sobre la copia de prod: el patron viejo (`recarg`, sin
    # acentos y sin la forma corta) no matcheaba ninguna de las tres, y 2.664 sesiones
    # CON comprobante del cliente quedaban fuera del gate por eso.
    for kw in ("Cargar como agente", "Hola cargar como agente", "Carga",
               "Hola recárgueme a la cuenta de agente",
               "Buenas tardes, ayúdeme acreditando"):
        assert has_recharge_context([_cli_txt(kw)]) is True, kw


def test_la_forma_corta_exige_frontera_y_terminacion_control_fp():
    # `carg` suelto matcheaba "descargar"/"encargado"/"a cargo": 1.095 mensajes del
    # cliente en la copia. Bajar la app, el encargado o quien esta a cargo no son
    # recargas. La frontera izquierda mas la terminacion [au] los descartan.
    for no in ("no puedo descargar la app", "el encargado me dijo que espere",
               "quien esta a cargo de esto"):
        assert has_recharge_context([_cli_txt(no)]) is False, no


def test_saldo_solo_NO_es_contexto_de_recarga():
    # DELIBERADAMENTE fuera del patron: "acreditame el saldo" es una recarga, pero
    # "cuanto tengo de saldo" es una consulta de `info`. Sumaba solo 571 sesiones y no
    # justifica el falso positivo. Si el negocio lo quiere, se decide aparte.
    assert has_recharge_context([_cli_txt("cuanto tengo de saldo?")]) is False


def test_el_pitch_del_operador_sigue_sin_contar_con_el_patron_nuevo():
    # Guard de no-regresion del criterio del 2026-08-06: el patron se amplio, pero
    # `has_recharge_context` sigue leyendo SOLO al cliente. La plantilla de venta dice
    # "primera carga" y ahora SI matchearia el patron -> el filtro de emisor es lo unico
    # que evita volver a inflar el gate un 41,4%.
    assert has_recharge_context(
        [_age_txt("con tu primera carga comienza a disfrutar")]) is False
    assert has_recharge_context(
        [_age_txt("te acredito la recarga en breve")]) is False
