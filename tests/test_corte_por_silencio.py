"""Seis horas de silencio cierran la interaccion, aunque el CRM nunca la haya cerrado.

LA MENTIRA QUE VIENE A MATAR. Hasta el 2026-08-24 `partir_en_interacciones` cortaba SOLO en
la nota `*resuelto*` del CRM. Si el operador nunca cerraba, todo el transcript era UNA
interaccion -- sin tope de ningun tipo. Y `assign_sessions` no lo salvaba: su `SPAN_CAP` de
12h corta entre EPISODIOS (filas de `conversations`), asi que cuando la sesion tiene un solo
episodio no hay frontera donde aplicarlo.

MEDIDO en la copia del 2026-08-24, sobre las 1.431 sesiones cerradas en 4 dias:
    88,1%  el stream cabe en las 12h del SPAN_CAP
    10,3%  lo pasa por MAS DE SIETE DIAS -- maximo 6.765 horas (282 dias)
    y 164 de esas 170 tienen UN SOLO episodio
Consecuencia en el tablero: filas que declaran "33 interacciones · 10 operadores" al lado de
una nota que juzgo tres minutos de UNA persona.

LA DECISION ES DEL NEGOCIO (2026-08-24): "que el tiempo sea solo de 6 horas por si alguien
escribio una respuesta, y de ahi que todo se agarren como interacciones diferentes, porque
cada interaccion tiene un operador a calificar". Las 6 horas son la GRACIA para la respuesta
tardia: dentro de la ventana el mensaje todavia pertenece a la interaccion; pasada, empieza
otra con su propio operador a calificar.

EL SILENCIO SE MIDE ENTRE MENSAJES REALES. Las notas no cuentan, y no es un detalle: el ETL
archiva notas en momentos que no describen la atencion (misma leccion que `_fin_de_actividad`
en src/sessions.py, donde una nota corrida al futuro mergeaba dos interacciones distintas).
"""
from datetime import datetime, timedelta, timezone

from src.interacciones import SILENCIO_MAX, partir_en_interacciones

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def _m(minutos, from_me, body="hola", *, is_note=False):
    return {"created_at": T0 + timedelta(minutes=minutos), "from_me": from_me,
            "is_note": is_note, "body": body, "sent_from": "1" if from_me else None,
            "user_id": "u1" if from_me else None, "media_type": None}


def test_la_ventana_es_de_seis_horas():
    assert SILENCIO_MAX == timedelta(hours=6)


def test_una_respuesta_dentro_de_las_seis_horas_sigue_siendo_LA_MISMA_interaccion():
    """Es para lo que existe la ventana: el operador tarda, pero contesto a lo que le
    escribieron. Partir aca dejaria al cliente en un fragmento sin respuesta -- una falla
    fabricada."""
    msgs = [_m(0, False, "me ayudas con una recarga?"),
            _m(5 * 60 + 55, True, "listo, ya está tu saldo")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1
    assert len(partes[0]) == 2


def test_pasadas_las_seis_horas_es_OTRA_interaccion():
    """EL CLIENTE es el que vuelve, y por eso son dos. Si el que hablara pasada la ventana
    fuera el OPERADOR, la regla de continuidad las pega -- contestar tarde es una atencion
    lenta, no un abandono (ver tests/test_continuidad_entre_fragmentos.py)."""
    msgs = [_m(0, False, "me ayudas con una recarga?"),
            _m(1, True, "listo, ya está tu saldo"),
            _m(6 * 60 + 2, False, "otra consulta")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2, "seis horas de silencio son dos atenciones distintas"
    assert partes[0][0]["body"] == "me ayudas con una recarga?"
    assert partes[1][0]["body"] == "otra consulta"


def test_el_corte_por_silencio_no_necesita_que_el_crm_haya_cerrado():
    """El caso de las 164 sesiones de un solo episodio: nadie cierra nunca, y sin este
    corte el stream no tiene tope."""
    msgs = [_m(0, False, "hola"), _m(2, True, "en qué te ayudo?"),
            _m(60 * 24 * 3, False, "buenas, otra consulta"),      # 3 dias despues
            _m(60 * 24 * 3 + 3, True, "decime"),
            _m(60 * 24 * 20, False, "y una más")]                 # 20 dias despues
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 3, "tres visitas separadas por dias son tres interacciones"


def test_cada_interaccion_queda_con_UN_operador_a_calificar():
    """El motivo que dio el negocio: "cada interaccion tiene un operador a calificar". Con
    el stream sin tope, dos operadores de dias distintos caian en la misma bolsa."""
    from src.metrics import primary_operator

    ayer = [_m(0, False, "me cargas 30?"),
            {**_m(2, True, "listo"), "user_id": "ana"}]
    hoy = [_m(60 * 30, False, "otra recarga"),
           {**_m(60 * 30 + 2, True, "hecho"), "user_id": "beto"}]
    partes = partir_en_interacciones(ayer + hoy)
    assert len(partes) == 2
    assert [primary_operator(p) for p in partes] == ["ana", "beto"]


def test_una_nota_en_el_medio_no_parte_la_interaccion():
    """El silencio se mide entre mensajes REALES: una nota archivada en el medio no es
    actividad de la atencion y no puede abrir una interaccion nueva."""
    msgs = [_m(0, False, "hola"),
            _m(1, True, "*Asignado automáticamente* a Ana", is_note=True),
            _m(2, True, "decime")]
    assert len(partir_en_interacciones(msgs)) == 1


def test_una_nota_tardia_NO_mantiene_viva_la_ventana():
    """El caso filoso: si la nota contara como actividad, el silencio entre el "decime" y el
    "¿algo más?" seria de 5 minutos y quedaria UNA interaccion. Son 7 horas de silencio
    real, asi que son dos -- y no se pegan porque la primera ya tiene negocio adentro."""
    msgs = [_m(0, False, "hola"), _m(1, True, "decime"),
            _m(60 * 7, True, "Ana *reabierto* la conversación", is_note=True),
            _m(60 * 7 + 5, True, "¿algo más?")]
    assert len(partir_en_interacciones(msgs)) == 2


def test_el_cierre_del_crm_sigue_cortando_antes_de_las_seis_horas():
    """La regla nueva SE SUMA, no reemplaza: el `*resuelto*` sigue siendo la frontera
    cuando el operador si cerro."""
    msgs = [_m(0, False, "me cargas 30?"), _m(2, True, "listo, ya está tu saldo"),
            _m(3, True, "Ana *resuelto* la conversación", is_note=True),
            _m(30, False, "gracias!"), _m(31, True, "un gusto")]
    assert len(partir_en_interacciones(msgs)) == 2


def test_la_gracia_del_comprobante_sobrevive():
    """`GRACIA_CIERRE_SEG` vive DENTRO de los 120 segundos, asi que la ventana de 6 horas no
    la puede pisar. Protege los 42 retiros que se calificaban con 2 estrellas por buscar un
    comprobante que estaba del otro lado de la frontera."""
    msgs = [_m(0, False, "quiero retirar"), _m(1, True, "listo"),
            {**_m(2, True, "Ana *resuelto* la conversación", is_note=True)},
            {**_m(2.5, True, None), "media_type": "image"}]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1, "el comprobante adjunto al cierre es el MISMO gesto"
    assert partes[0][-1]["media_type"] == "image"
