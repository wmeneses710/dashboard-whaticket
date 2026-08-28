"""La respuesta que quedo del otro lado de la frontera se pega a lo que respondio.

EL DAÑO QUE ESTE TEST CIERRA, MEDIDO ANTES DE ESCRIBIRLO. Partir en interacciones es lo que
el negocio decidio el 2026-08-24 ("cada interaccion tiene un operador a calificar"), pero
medido sobre 1.200 sesiones multi-episodio de la copia, de 3.404 interacciones resultantes
**806 (23,68%) no tienen respuesta del negocio adentro** -- y de esas **254 tienen la
acreditacion en el fragmento VECINO**. Calificar ese fragmento solo le pone 1 estrella a
quien SI hizo el trabajo. En agosto se estimaron 25 casos y por eso se rechazo partir
(docs/handoff.md §10); son diez veces mas.

LA REGLA. Un fragmento sin NINGUN mensaje del negocio no es una atencion propia si el
negocio habla al principio del fragmento siguiente: esas palabras contestan lo que el
cliente acaba de decir, asi que las dos mitades son la MISMA atencion y el CRM las corto al
medio. Se pegan.

ES LA GENERALIZACION DE `GRACIA_CIERRE_SEG`, que ya hacia esto mismo hacia adelante con el
comprobante que el operador adjunta despues de cerrar (42 retiros con 2 estrellas por buscar
evidencia del otro lado de la frontera). Aca la evidencia viaja al reves: el cliente quedo
en un fragmento y la respuesta en el que sigue.

POR QUE UN SOLO SALTO Y NO UNA CADENA. Si el fragmento siguiente ARRANCA con el cliente, el
cliente volvio -- eso es una visita nueva por definicion del negocio, y el fragmento anterior
si quedo sin responder. Encadenar hacia atras reconstruiria justo el stream sin tope que el
corte de 6h vino a matar (10,3% de las sesiones pasaban los 7 dias, maximo 282).

EL CASO DEL VECINO ANTERIOR NO ENTRA ACA, y no es un olvido: "el operador acredito, el
cliente dijo gracias, nadie le contesto" ya tiene su rubrica -- `solo_cortesia`, con el
estandar de cierre (4 estrellas si cerro bien, 3 si dejo al cliente colgado). De los 806
fragmentos, 239 son exactamente eso.
"""
from datetime import datetime, timedelta, timezone

from src.interacciones import partir_en_interacciones

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def _m(minutos, from_me, body="hola", *, is_note=False, media_type=None, user_id=None):
    return {"created_at": T0 + timedelta(minutes=minutos), "from_me": from_me,
            "is_note": is_note, "body": body,
            "sent_from": "OPERATOR" if (from_me and not is_note) else None,
            "user_id": user_id or ("u1" if from_me else None), "media_type": media_type}


def _nota_cierre(minutos, quien="Ana"):
    return _m(minutos, True, f"{quien} *resuelto* la conversación", is_note=True)


# --- el caso de los 254 ---------------------------------------------------------------

def test_el_comprobante_y_su_acreditacion_no_se_parten_por_un_cierre_del_crm():
    """El CRM cierra DESPUES de que el cliente manda el comprobante y ANTES de que el
    operador acredite. Sin la regla son dos interacciones: una con 1 estrella para quien
    acredito, y otra sin cliente."""
    # La acreditacion llega 9 MINUTOS despues del cierre: fuera de `GRACIA_CIERRE_SEG` (120s),
    # que es la que ya cubria el comprobante adjuntado en el mismo gesto. Ahi vivian los 254.
    msgs = [_m(0, False, None, media_type="image"),      # el comprobante
            _nota_cierre(1),
            _m(10, True, "listo, ya está tu saldo disponible")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1, "el comprobante y su acreditación son la MISMA atención"
    assert partes[0][0]["media_type"] == "image"
    assert "saldo" in partes[0][-1]["body"]


def test_una_respuesta_tardia_es_UNA_atencion_lenta_no_un_abandono():
    """Pasadas las 6h el corte dispara, pero el operador SI contesto. La nota honesta es
    "tardó", no "nadie le respondió": esta es la unica que puede acusar de no responder."""
    msgs = [_m(0, False, "me ayudas con una recarga?"),
            _m(60 * 9, True, "perdón la demora, ya te cargo")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1
    assert len(partes[0]) == 2


def test_si_el_cliente_es_el_que_vuelve_el_fragmento_SI_quedo_sin_responder():
    """La falla real se conserva entera: el cliente escribio, nadie contesto, y volvio al
    otro dia. Eso son dos visitas y la primera quedo sin respuesta."""
    msgs = [_m(0, False, "hola, están?"),
            _m(60 * 30, False, "sigo esperando"),
            _m(60 * 30 + 2, True, "perdón, acá estoy")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert not any(m.get("from_me") and not m.get("is_note") for m in partes[0]), (
        "la primera visita tiene que seguir sin respuesta del negocio")


def test_no_se_pega_una_cadena_hacia_atras():
    """Dos visitas del cliente sin respuesta y despues el operador: solo la ULTIMA se pega.
    Encadenar reconstruiria el stream sin tope."""
    msgs = [_m(0, False, "primera vez"),
            _m(60 * 20, False, "segunda vez"),
            _m(60 * 20 + 1, True, "te contesto esta")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert partes[0][0]["body"] == "primera vez"
    assert [m["body"] for m in partes[1]] == ["segunda vez", "te contesto esta"]


def test_la_nota_del_crm_no_cuenta_como_respuesta_del_negocio():
    """Es la trampa central: la nota es `from_me` pero NO es un mensaje al cliente. Si
    contara, el fragmento parecería respondido y no se pegaría con quien SI contesto. Misma
    leccion que en src/sin_respuesta.py y en `cliente_tuvo_la_ultima_palabra`."""
    msgs = [_m(0, False, "mi retiro?"),
            _nota_cierre(1),
            _m(60 * 8, True, "ya está pagado")]
    assert len(partir_en_interacciones(msgs)) == 1


def test_dos_atenciones_completas_siguen_siendo_dos():
    """La regla no puede mergear lo que ya estaba bien: los dos fragmentos tienen negocio
    adentro, asi que ninguno es una continuacion del otro."""
    msgs = [_m(0, False, "me cargas 30?"), _m(1, True, "listo", user_id="ana"),
            _nota_cierre(2, "Ana"),
            _m(60 * 20, False, "otra recarga"),
            _m(60 * 20 + 1, True, "hecho", user_id="beto")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2


def test_un_fragmento_final_sin_respuesta_no_tiene_donde_pegarse():
    """No hay fragmento siguiente: la falla es real y se queda."""
    msgs = [_m(0, False, "me cargas 30?"), _m(1, True, "listo"),
            _nota_cierre(2),
            _m(60 * 20, False, "hola? alguien?")]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2
    assert not any(m.get("from_me") and not m.get("is_note") for m in partes[1])


# --- una sola fuente de verdad para "respondio el negocio?" ---------------------------

def test_la_regla_de_respuesta_del_negocio_es_LA_MISMA_que_la_de_sin_respuesta():
    """`interacciones` no puede importar `sin_respuesta` (arrastraria `scorer` a un modulo
    de base), asi que el predicado esta escrito dos veces. Este test es lo que impide que
    se separen: si una empieza a contar las notas y la otra no, el fragmento se pega mal y
    la estrella se fabrica igual."""
    from src.interacciones import _hubo_negocio
    from src.sin_respuesta import hubo_respuesta_del_negocio

    casos = [
        [],
        [_m(0, False, "hola")],
        [_m(0, True, "hola")],
        [_nota_cierre(0)],
        [_m(0, False, "hola"), _nota_cierre(1)],
        [_m(0, False, "hola"), _m(1, True, "decime")],
        [_m(0, True, None, media_type="image")],
    ]
    for c in casos:
        assert _hubo_negocio(c) == hubo_respuesta_del_negocio(c), c


# --- LA COLA DE CORTESIA: el "gracias" despues del cierre ------------------------------
#
# EL CASO REAL que lo destapo (sesion 66e68dcc, 15-ago, revisando el rescore por interaccion):
#     21:36:10  OPERADOR  "tu saldo ya esta disponible"   <- Michelle acredito bien
#     21:36:12  NOTA      Michelle *resuelto*
#     21:36:33  CLIENTE   "Ok listo bendiciones"          <- 21 SEGUNDOS despues
#     21:36:34  NOTA      *Asignado automaticamente* a Michelle   <- el CRM reabre SOLO
#     21:37:46  NOTA      Michelle *resuelto*             <- cierra sin escribir
# Se cortaba como atencion nueva, sin respuesta del operador -> **1 estrella a Michelle**
# por "nadie le respondio", 21 segundos despues de atender perfecto.
#
# `_pegar_continuaciones` NO lo agarra: pega hacia ADELANTE (fragmento sin negocio ->
# siguiente que arranca con el negocio). Aca la evidencia va al reves: el "gracias" es la
# COLA de la anterior, no la cabeza de la siguiente.
#
# Y NO ES SOLO EVITAR UN 1 ESTRELLA FALSO. `store.py` dice que
# `signals.cliente_confirmo_resuelto` -- el cliente diciendo "ya pude, gracias" -- es
# **ground truth del unico que sabe si su problema se resolvio**. Cortarlo afuera le ROBA a
# la atencion anterior su mejor evidencia.
#
# EL UMBRAL SALE DEL DATO, sobre 717 "gracias" tras un cierre en 30 dias:
#     <= 1 min   270 casos   88,1 por ciento mismo operador
#     1-5 min    151 casos   88,1 por ciento
#     5-15 min   125 casos   80,8 por ciento
#     > 15 min   171 casos   55,0 por ciento   <- moneda al aire: ya es otra visita
# A los 5 minutos la continuidad todavia se sostiene; pasados los 15 se cae a la mitad.

def test_el_gracias_del_cliente_se_pega_a_la_atencion_QUE_LO_GANO():
    """El caso de Michelle, tal cual paso."""
    msgs = [
        _m(0, False, "les mando el comprobante", media_type="image"),
        _m(1, True, "*Michelle:* Estamos verificando tu comprobante"),
        _m(3, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(3, "Michelle"),
        _m(3.5, False, "Ok listo bendiciones"),      # 30 s despues del cierre
        _nota_cierre(5, "Michelle"),
    ]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 1, (
        f"el 'gracias' quedo como atencion aparte ({len(partes)} fragmentos): eso le pone "
        f"1 estrella a quien acaba de acreditar bien"
    )
    textos = " ".join(m["body"] for m in partes[0])
    assert "Ok listo bendiciones" in textos, "se pego pero se perdio el mensaje del cliente"


def test_el_gracias_TARDIO_sigue_siendo_una_visita_nueva():
    """Pasados los 15 minutos el mismo operador es moneda al aire (55 por ciento): el
    cliente volvio de verdad y la regla del negocio manda -- visita nueva."""
    msgs = [
        _m(0, False, "les mando el comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(40, False, "gracias"),                    # 39 minutos despues
    ]
    assert len(partir_en_interacciones(msgs)) == 2, (
        "pego un 'gracias' de 39 minutos despues: a esa distancia ya es otra visita"
    )


def test_un_RECLAMO_despues_del_cierre_NO_se_pega():
    """Los 102 casos de contenido real. Ahi el cliente dijo algo, nadie le contesto, y ese
    1 estrella esta BIEN puesto: es justo lo que la regla vino a hacer visible."""
    msgs = [
        _m(0, False, "les mando el comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(2, False, "no me llego nada, sigo sin ver el saldo en mi cuenta"),
    ]
    assert len(partir_en_interacciones(msgs)) == 2, (
        "pego un RECLAMO como si fuera cortesia: eso esconde una atencion que nadie dio"
    )


def test_no_se_pega_si_el_operador_SI_contesto_el_gracias():
    """Si el operador respondio, el fragmento es una atencion propia con su trabajo."""
    msgs = [
        _m(0, False, "comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(2, False, "gracias"),
        _m(2.5, True, "*Michelle:* un placer, cualquier cosa avisame"),
        _nota_cierre(3, "Michelle"),
    ]
    partes = partir_en_interacciones(msgs)
    assert len(partes) == 2, "se comio una atencion en la que el operador si trabajo"


# --- LA VENTANA SE ABRE A 10 MINUTOS (decision del negocio, 2026-08-28) ----------------
#
# El umbral arranco en 5 minutos (`GRACIA_CORTESIA_SEG = 300`) tomando el corte conservador
# de la tabla de arriba. MEDIDO despues, con codigo de produccion sobre `messages` crudo
# (`partir_en_interacciones` en vivo, NO sobre notas guardadas): de los fragmentos sin
# respuesta del negocio que cobran 1 estrella, **68 de 92 (73,9 por ciento) son cortesia** --
# 'Muchas gracias', '🫡', 'Gracias bro' -- y NO fallan por la palabra: la palabra matchea.
# Fallan por el TIEMPO, del otro lado de los 5 minutos.
#
# La banda 5-15 min conserva 80,8 por ciento de continuidad (125 casos), asi que el dato ya
# respaldaba abrirla. Se abre a 10 y no a 15: el negocio elige quedarse en la mitad de la
# banda medida, dejando el margen del 55 por ciento (>15 min) bien afuera.


def test_el_gracias_a_los_OCHO_minutos_se_pega():
    """La banda 5-15 conserva 80,8 por ciento de continuidad: a esa distancia sigue siendo
    la cola de la atencion que la gano, no una visita nueva."""
    msgs = [
        _m(0, False, "les mando el comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(9, False, "Muchas gracias"),               # 8 minutos despues del cierre
    ]
    assert len(partir_en_interacciones(msgs)) == 1, (
        "no pego un 'gracias' de 8 minutos: le pone 1 estrella por 'nadie le respondio' a "
        "quien acaba de acreditar bien, y ese es el 73,9 por ciento de los falsos 1 estrella"
    )


def test_el_gracias_JUSTO_en_el_limite_de_diez_minutos_se_pega():
    """El borde exacto de la ventana, que es `<=`."""
    msgs = [
        _m(0, False, "comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(11, False, "gracias"),                     # exactamente 10 minutos
    ]
    assert len(partir_en_interacciones(msgs)) == 1, "el limite es <=: a los 10 min pega"


def test_pasados_los_diez_minutos_el_gracias_NO_se_pega():
    """El control negativo del cambio: la ventana se abrio, no se elimino."""
    msgs = [
        _m(0, False, "comprobante", media_type="image"),
        _m(1, True, "*Michelle:* tu saldo ya esta disponible"),
        _nota_cierre(1, "Michelle"),
        _m(12, False, "gracias"),                     # 11 minutos: afuera
    ]
    assert len(partir_en_interacciones(msgs)) == 2, (
        "pego un 'gracias' de 11 minutos: la ventana se abrio a 10, no se elimino"
    )
