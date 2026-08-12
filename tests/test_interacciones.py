"""La frontera de una interaccion sale del OBJETO: la nota de cierre del operador.

Criterio del negocio (2026-08-11): "una interaccion es cuando un usuario habla con el
operador, es este quien decide cuando cerrar, esto deberia estar en el objeto". Y estaba:
el CRM escribe "<Nombre> *resuelto* la conversacion" como nota interna (409.820 de esas).
Nadie lo veia porque TODAS las rubricas filtran `is_note` en su primera linea.
"""
from datetime import datetime, timedelta, timezone

from src.interacciones import (
    es_cierre,
    interaccion_de,
    partir_en_interacciones,
    tiempos_de,
)

BASE = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)


def _cli(min_, body="", media="chat"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(min_, body):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


def _cierre(min_, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def _asignado(min_, quien="Mario"):
    return {"created_at": BASE + timedelta(minutes=min_), "from_me": True,
            "is_note": True, "body": f"*Asignado automáticamente* a {quien}"}


def test_reconoce_la_nota_de_cierre_y_solo_esa():
    assert es_cierre(_cierre(0)) is True
    assert es_cierre(_asignado(0)) is False
    assert es_cierre({"is_note": True, "body": "Mario *aceptado* la conversación"}) is False
    assert es_cierre({"is_note": True, "body": "Mario *reabierto* la conversación"}) is False
    # un mensaje REAL que diga la palabra no es un cierre: la frontera es la NOTA
    assert es_cierre(_op(0, "ya lo dejo *resuelto*")) is False


def test_sin_cierre_todo_es_UNA_interaccion():
    # El 96,3% de las conversaciones cae aca: un solo cierre (o ninguno) = una interaccion.
    msgs = [_cli(0, "me recarga"), _op(1, "listo")]
    assert len(partir_en_interacciones(msgs)) == 1


def test_cada_cierre_abre_una_interaccion_nueva():
    msgs = [_cli(0, "me recarga"), _op(1, "acreditado"), _cierre(2),
            _cli(60, "otra recarga"), _op(61, "acreditado"), _cierre(62)]
    inter = partir_en_interacciones(msgs)
    assert len(inter) == 2
    assert inter[0][0]["body"] == "me recarga"
    assert inter[1][0]["body"] == "otra recarga"


def test_una_interaccion_sin_mensajes_reales_no_cuenta():
    # Arranca con la asignacion y cierra sin que nadie hable: no es una interaccion.
    msgs = [_asignado(0), _cierre(1),
            _cli(10, "me recarga"), _op(11, "acreditado"), _cierre(12)]
    inter = partir_en_interacciones(msgs)
    assert len(inter) == 1
    assert inter[0][0]["body"] == "me recarga"


def test_las_notas_viajan_dentro_de_la_interaccion():
    # No se le saca informacion a nadie: quien filtra `is_note` es cada rubrica.
    msgs = [_asignado(0), _cli(1, "hola"), _op(2, "listo"), _cierre(3)]
    assert len(partir_en_interacciones(msgs)[0]) == 4


def test_interaccion_de_acota_la_busqueda_al_comprobante_correcto():
    # EL CASO `f9b31f4f`: un comprobante que nadie contesto, y la acreditacion de OTRA
    # transaccion dias despues. Sin acotar, se emparejaban.
    huerfano = _cli(0, "", media="image")
    msgs = [huerfano, _cierre(5),
            _cli(4800, "", media="image"), _cli(4801, "me recarga"),
            _op(4802, "gracias por tu recarga, tu saldo ya esta disponible"), _cierre(4803)]
    ventana = interaccion_de(msgs, huerfano)
    assert any(m is huerfano for m in ventana)
    cuerpos = " ".join((m.get("body") or "") for m in ventana)
    assert "saldo ya esta disponible" not in cuerpos, \
        "la acreditacion de otra interaccion no puede caer en esta ventana"


# --- CERRAR-Y-ADJUNTAR ES UN SOLO GESTO -------------------------------------------
# Hallado el 2026-08-12 auditando el rescore v5, y es un efecto colateral del propio corte
# por interaccion. El flujo REAL del operador de retiro es cerrar la conversacion y adjuntar
# el comprobante inmediatamente despues: la nota de cierre y la imagen salen en el mismo
# gesto, con una MEDIANA DE 1,1 SEGUNDOS de diferencia. Como el corte era estricto, ese
# comprobante caia en la interaccion SIGUIENTE y `calificar_retiro` no lo encontraba nunca
# -> "nunca envio el comprobante", 2 estrellas, para un retiro que SI se pago.
# TAMAÑO MEDIDO: 42 de 139 retiros en 2 estrellas con esa frase (el 32%), el 100% de ellos
# con el comprobante dentro de los 2 minutos del cierre.
# Testigo: 98591993-09c3-470f-8bf3-b37804ffcfa6 -- nota de cierre 18:02:13,772, imagen
# "Transferencia exitosa 🍀 Este es el comprobante 👍" a las 18:02:14,379.
#
# La gracia es solo para el OPERADOR: si el que habla despues del cierre es el CLIENTE, eso
# SI es una interaccion nueva (es la definicion del negocio -- el cliente vuelve a hablar).

def _op_seg(seg, body, media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": media}


def _cierre_seg(seg, quien="Mario"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": True, "body": f"{quien} *resuelto* la conversación"}


def test_el_comprobante_que_llega_justo_despues_del_cierre_es_de_ESA_interaccion():
    comprobante = _op_seg(4, "Transferencia exitosa 🍀 Este es el comprobante 👍", "image")
    msgs = [_cli(0, "Monto a retirar: $100"),
            _op_seg(2, "Tu retiro está en proceso 🔄"),
            _cierre_seg(3),
            comprobante]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1
    assert comprobante in partes[0]
    # Y por lo tanto la rubrica lo encuentra al acotar por el pedido del cliente.
    assert comprobante in interaccion_de(msgs, msgs[0])


def test_la_gracia_no_se_estira_mas_alla_de_dos_minutos():
    tardio = _op_seg(200, "Este es el comprobante 👍", "image")
    msgs = [_cli(0, "Monto a retirar: $100"), _op_seg(2, "en proceso"),
            _cierre_seg(3), tardio]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert tardio not in partes[0]


def test_si_el_que_habla_tras_el_cierre_es_el_CLIENTE_es_otra_interaccion():
    # La definicion del negocio: una interaccion es el cliente volviendo a hablar.
    vuelve = _cli(0, "otra cosa mas")
    vuelve["created_at"] = BASE + timedelta(seconds=4)
    msgs = [_cli(0, "Monto a retirar: $100"), _op_seg(2, "en proceso"),
            _cierre_seg(3), vuelve, _op_seg(5, "dale")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert vuelve in partes[1]


def test_el_broadcast_post_cierre_no_abre_una_interaccion_fantasma():
    # El caso Italo (ec562888): la nota de cierre y el broadcast del canal salen en el
    # MISMO segundo. Sin la gracia, ese broadcast quedaba solo en una interaccion aparte.
    msgs = [_cli(0, "Ayúdame con la cta de guayaquil"), _op_seg(10, "BANCO GUAYAQUIL"),
            _cierre_seg(60), _op_seg(60, "Agente, revisa nuestro canal oficial")]
    assert len(partir_en_interacciones(msgs)) == 1


# --- UN CIERRE QUE REBOTA NO ES UNA FRONTERA --------------------------------------
# Hallado el 2026-08-12 auditando el rescore v5. El CRM a veces dispara "*resuelto*" y lo
# "*reabierto*" segundos despues, sin que nadie haya hablado en el medio: el cierre NO PEGO.
# Tratarlo como frontera parte la interaccion al medio y deja al operador con una
# interaccion donde "nadie respondio".
# Testigo `3ba0da9d`: llega el audio con el comprobante, a los 56 s salta un "*resuelto*"
# sin una sola palabra del operador, 11 s despues "*reabierto*", y AHI se atiende bien y se
# cierra de verdad 28 min mas tarde con "Tu saldo ya esta disponible". La nota usaba el
# primer cierre: `resolution_seconds=56.8` y 1 estrella, cuando el servicio fue excelente.
# MEDIDO sobre la base entera: 7.406 pares resuelto->reabierto, MEDIANA 58,5 segundos, y
# 4.884 (66%) dentro de los 2 minutos. La cola larga (promedio 10 h) son reaperturas
# legitimas, y esas SI tienen que cortar.

def _reabierto_seg(seg, quien="Mario"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": True, "body": f"{quien} *reabierto* la conversación"}


def test_un_cierre_reabierto_al_toque_NO_parte_la_interaccion():
    msgs = [_cli(0, "", media="audio"),
            _cierre_seg(56),
            _reabierto_seg(67),
            _op_seg(100, "Estamos verificando tu comprobante"),
            _op_seg(1700, "¡Gracias por tu recarga! Tu saldo ya está disponible"),
            _cierre_seg(1710)]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1


def test_una_reapertura_LEJANA_si_es_una_interaccion_nueva():
    # La cola larga: el cliente volvio al dia siguiente. Ese cierre fue real.
    msgs = [_cli(0, "Monto a retirar: $50"), _op_seg(30, "en proceso"),
            _cierre_seg(60),
            _reabierto_seg(40000),
            _cli(40010, "hola otra vez"), _op_seg(40030, "dime")]
    assert len(partir_en_interacciones(msgs)) == 2


def test_si_alguien_HABLO_antes_de_la_reapertura_el_cierre_fue_real():
    # El cliente volvio a escribir y eso reabrio: interaccion nueva, no un rebote del CRM.
    vuelve = _cli(0, "una cosa mas")
    vuelve["created_at"] = BASE + timedelta(seconds=70)
    msgs = [_cli(0, "Monto a retirar: $50"), _op_seg(30, "en proceso"),
            _cierre_seg(60), vuelve, _reabierto_seg(75), _op_seg(90, "dime")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert vuelve in partes[1]


# --- LOS TIEMPOS TIENEN QUE DESCRIBIR LA INTERACCION QUE SE JUZGO ------------------
# MEDIDO el 2026-08-12 sobre el rescore v5. Los tiempos que se guardan salen de los campos
# del CRM, que describen el ENVASE y no el trabajo: `created_at` es el primer mensaje de la
# conversacion entera, `first_sent_message_at` el primer mensaje del operador de toda la
# conversacion, y `resolved_at` el ULTIMO cierre. En una conversacion con varias
# interacciones cada campo sale de una interaccion DISTINTA.
# Caso `f9b31f4f` (17 interacciones): created_at de la 1ra, first_sent_message_at de la 2da
# (51,5 h despues), assigned_at y resolved_at de la ULTIMA. Cuatro interacciones, una fila.
# TAMAÑO: 1.208 sesiones de `jugador` (10,2%) tienen varias interacciones. La resolucion
# mostrada pasa de 3,4 h (una interaccion) a 88,5-271,3 h. Contra la ventana realmente
# juzgada: p90 de 3.834x, y el peor muestra 2.157,6 h para una interaccion de 0,1 minutos.

def test_tiempos_de_describe_la_interaccion_no_la_conversacion():
    inter = [_cli(0, "Monto a retirar: $50"),
             _asignado(0),
             _op(2, "en proceso"),
             _op(5, "acá tenés el comprobante"),
             _cierre(6)]
    inicio, primera_op, cierre = tiempos_de(inter)
    assert inicio == BASE                              # el primer mensaje REAL del cliente
    assert primera_op == BASE + timedelta(minutes=2)   # la primera respuesta del operador
    assert cierre == BASE + timedelta(minutes=6)       # la nota de cierre


def test_tiempos_sin_nota_de_cierre_usan_el_ultimo_mensaje_real():
    inter = [_cli(0, "hola"), _op(3, "dale")]
    inicio, primera_op, cierre = tiempos_de(inter)
    assert inicio == BASE
    assert primera_op == BASE + timedelta(minutes=3)
    assert cierre == BASE + timedelta(minutes=3)


def test_tiempos_sin_respuesta_del_operador_dan_None_en_la_primera():
    # No inventa un tiempo: si nadie contesto, no hay primera respuesta.
    inter = [_cli(0, "hola"), _cierre(1)]
    inicio, primera_op, cierre = tiempos_de(inter)
    assert inicio == BASE
    assert primera_op is None
    assert cierre == BASE + timedelta(minutes=1)


def test_tiempos_ignora_las_notas_para_el_inicio():
    # La nota de asignacion automatica no es el arranque de la atencion.
    inter = [_asignado(0), _cli(1, "hola"), _op(2, "dale")]
    inicio, _, _ = tiempos_de(inter)
    assert inicio == BASE + timedelta(minutes=1)


def test_tiempos_de_una_interaccion_vacia_no_revienta():
    assert tiempos_de([]) == (None, None, None)
    assert tiempos_de([_asignado(0)]) == (None, None, None)


def test_un_ancla_desconocida_degrada_al_transcript_completo():
    msgs = [_cli(0, "hola"), _cierre(1)]
    assert interaccion_de(msgs, _cli(999, "no estoy")) is msgs
