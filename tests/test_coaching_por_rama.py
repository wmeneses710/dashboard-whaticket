"""El consejo tiene que hablar de la rama que produjo la nota, no de la nota.

POR QUE EXISTE. Los `_COACHING` estaban indexados por ESTRELLAS, pero las rubricas llegan
a 2★ por DOS caminos distintos en cada motivo transaccional. El texto asumia siempre el
peor caso, asi que le decia al operador que no hizo algo que SI habia hecho — con su propio
`rating_rationale` al lado diciendo lo contrario.

MEDIDO el 2026-08-11 sobre la copia de produccion:
  - `deposito`: 370 de 1.400 sesiones en 2★ (26,4%) tienen `acredito=true` y recibian
    "Confirmale siempre al cliente que la plata entro" — el texto de "nunca confirmo".
    Caso a4b93151: el rationale dice "Confirmo la acreditacion, pero tardo 6 minutos".
  - `retiro`: 112 de 221 (50,7%) entregaron el comprobante y recibian "El retiro quedo
    sin comprobante".
  - `soporte_cuenta`: el texto del 2 usa un "o" ("La atencion fue lenta O no llego a nada
    concreto") que hace imposible saber cual de las dos fallo. Caso 0b321579 (Blanca
    Vera): la atencion NO fue lenta (3,4 min de mediana efectiva) y el consejo igual lo
    sugeria.
"""
from datetime import datetime, timedelta, timezone

from src.deposito import score_deposito
from src.retiro import score_retiro
from src.soporte import score_soporte

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)


def _cli(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(seg, body, media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": media}


# --- deposito: las dos ramas del 2 -----------------------------------------------

def test_deposito_NUNCA_confirmo_pide_confirmar():
    r = score_deposito([_cli(0, "les mando el comprobante de la recarga"),
                        _cli(0, "", media="image"),
                        _op(30, "Estamos verificando tu comprobante")])
    assert r.stars == 2
    assert "confirm" in r.recomendacion.lower()


def test_deposito_confirmo_TARDE_habla_del_tiempo_no_de_confirmar():
    # Confirmo de verdad; el consejo NO puede decirle que nunca confirma.
    r = score_deposito([_cli(0, "les mando el comprobante de la recarga"),
                        _cli(0, "", media="image"),
                        _op(600, "Listo"),
                        _op(601, "Gracias por tu recarga, tu saldo ya esta disponible")])
    assert r.stars == 2, r.rating_rationale
    consejo = r.recomendacion.lower()
    assert "nunca" not in consejo
    assert "confirmale siempre" not in consejo
    assert any(p in consejo for p in ("tard", "minuto", "acuse", "enseguida", "aviso"))


# --- retiro: las dos ramas del 2 -------------------------------------------------

def test_retiro_SIN_comprobante_pide_el_comprobante():
    r = score_retiro([_cli(0, "Monto a retirar: 70"),
                      _op(30, "Tu retiro esta en proceso")])
    assert r.stars == 2
    assert "comprobante" in r.recomendacion.lower()


def test_retiro_que_SI_entrego_pero_tarde_no_dice_que_quedo_sin_comprobante():
    # entrega a los 45 min: pasa ENTREGA_TOPE (30 min) -> cae en el 2, rama "tarde".
    r = score_retiro([_cli(0, "Monto a retirar: 70"),
                      _op(30, "dale, lo proceso"),
                      _op(2700, "listo, aca tienes", media="image")])
    assert r.stars == 2, r.rating_rationale
    consejo = r.recomendacion.lower()
    assert "quedo sin comprobante" not in consejo and "quedó sin comprobante" not in consejo
    assert any(p in consejo for p in ("tard", "minuto", "tiempo", "demor"))


# --- soporte: el "o" ambiguo ------------------------------------------------------

def test_soporte_sin_salida_concreta_habla_SOLO_de_la_salida():
    r = score_soporte([_cli(0, "no puedo entrar a mi cuenta"),
                       _op(30, "Hola que tal buenos dias amiga")])
    assert r.stars == 2
    consejo = r.recomendacion.lower()
    assert " o " not in consejo, "el 'o' hace imposible saber que fallo"
    assert "lenta" not in consejo, "la atencion fue rapida: no puede decir que fue lenta"


def test_soporte_lento_YA_NO_EMITE_CONSEJO():
    """CAMBIO DEL 2026-08-21. La rama emitia "Conviene avisar antes de cada consulta interna"
    y el manual no lo respalda (cero hits; lo que dice sobre escalamiento es todo INTERNO).
    Se retiro por decision del negocio -- 373 recomendaciones -- y la rama se queda SIN
    consejo en vez de con uno generico de relleno.

    Lo que ESTE test cuidaba sigue cuidado en otro lado: que el consejo hablara solo del
    tiempo y no metiera dos temas con un " o ". Ahora no hay consejo, asi que la unica
    afirmacion posible es que este vacio. El DIAGNOSTICO del tiempo no se perdio: vive en el
    rationale, que este test tambien verifica."""
    r = score_soporte([_cli(0, "no puedo entrar a mi cuenta"),
                       _op(3600, "ingresa de nuevo y probamos")])
    assert r.stars == 2
    assert r.recomendacion == ""
    # el tiempo sigue nombrado donde corresponde: en el rationale
    assert any(p in r.rating_rationale.lower()
               for p in ("tard", "esper", "minuto", "tiempo")), r.rating_rationale


# LA RAMA DEL RECHAZO tiene su propio consejo. Sin esto, un 4 de rechazo recibia el consejo
# del 4 normal ("la segunda duda suele ser el bono o el proximo deposito"), que en una recarga
# RECHAZADA no aplica: la segunda duda es como arreglar la boleta. Y un 3 de rechazo recibia
# el del 3 normal ("un primer mensaje corto apenas entra el comprobante"), cuando el problema
# no fue el acuse sino la demora en avisar el rechazo.

def test_rechazo_rapido_apunta_a_COMO_ARREGLARLO():
    r = score_deposito([_cli(0, "les mando el comprobante de la recarga"),
                        _cli(0, "", media="image"),
                        _op(1, "Titular incorrecto, la cuenta debe estar a tu nombre")])
    assert r.stars == 4, r.rating_rationale
    consejo = r.recomendacion.lower()
    assert "bono" not in consejo, "el consejo del 4 normal no aplica a un rechazo"
    assert any(p in consejo for p in ("arregl", "corregir", "verificar", "próximo intento",
                                      "proximo intento")), consejo


def test_rechazo_TARDE_apunta_a_la_DEMORA_del_aviso():
    r = score_deposito([_cli(0, "les mando el comprobante de la recarga"),
                        _cli(0, "", media="image"),
                        _op(8 * 60, "Boleta repetida")])   # 8 MINUTOS (helper en seg)
    assert r.stars == 3, r.rating_rationale
    consejo = r.recomendacion.lower()
    assert "en camino" in consejo or "esperando" in consejo or "enseguida" in consejo, consejo
