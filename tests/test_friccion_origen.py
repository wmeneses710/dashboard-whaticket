"""El texto de la friccion tiene que decir la VERDAD sobre su origen.

`friccion` se arma con dos señales que NO prueban lo mismo (src/scorer.py):

    friccion = (reasked or cliente_reinsistio) and not resolved

  - `client_reasked` es DETERMINISTA: exige silencio real del operador medido con
    timestamps (ver tests/test_friccion_silencio.py). Prueba que nadie contesto.
  - `cliente_reinsistio` es juicio LIBRE del modelo: "el cliente volvio a escribir porque
    no obtuvo respuesta". NO exige que el operador haya estado callado.

El `or` se agrego A PROPOSITO en v14 y no se discute aca: medido el 2026-08-13, 87 filas
tenian `cliente_reinsistio=true` con `friccion=false` y 71 de ellas (81,6%) estaban en 4 y
5 estrellas. Que el juicio del modelo pueda demotar es la decision tomada.

LO QUE ESTA MAL ES EL TEXTO. El error que se le muestra al operador dice siempre
"sin respuesta del operador", tambien cuando la señal dura dice que SI hubo respuesta.

MEDIDO el 2026-08-14 sobre v15, corriendo `client_reasked` sobre los mensajes reales de
las filas con `friccion=true` en el camino LLM: **57 filas, y 36 (63,2%) tienen
`client_reasked()=False`** -- 32 en 2 estrellas y 4 en 1. El caso que lo destapo
(`43df99b7`) tiene al operador contestando CADA UNO de los ~10 mensajes del cliente, y el
sistema le anota igual que no respondio.
"""
from datetime import datetime, timedelta, timezone

from src.scorer import score_by_motivo

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)

SIN_RESPUESTA = "El cliente tuvo que reinsistir sin respuesta del operador."


def _cli(seg, body="necesito ayuda"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": "chat"}


def _op(seg, body="ya te ayudo"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": "chat"}


# El operador contesta CADA mensaje: `client_reasked` da False (no hay silencio).
ATENDIDO_SIEMPRE = [
    _cli(0, "hola tengo un problema con la app"),
    _op(30, "contame que te pasa"),
    _cli(60, "no me carga la pantalla"),
    _op(90, "proba cerrando y abriendo la app"),
    _cli(120, "sigue igual"),
    _op(150, "te paso otro link entonces"),
]

# Silencio REAL de mas de 5 minutos con el cliente insistiendo: `client_reasked` da True.
CALLADO_DE_VERDAD = [
    _cli(0, "hola necesito que me ayuden"),
    _cli(400, "hola?"),
    _cli(900, "sigue ahi alguien"),
    _cli(1500, "por favor respondan"),
]


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over):
    resp = {
        "motivo": "problema",
        "dimensions": {"resolucion": "atendio", "iniciativa": "no ofrecio nada extra",
                       "cortesia": "cordial", "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "cliente_reinsistio": True,
        "rating_rationale": "el cliente tuvo que repetir",
        "recomendacion": "responde antes",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


def _errores(r):
    return list((r.dimensions or {}).get("errores") or [])


# --- el texto segun el origen ----------------------------------------------------

def test_con_silencio_medido_el_texto_dice_que_no_hubo_respuesta():
    r = score_by_motivo(target_messages=CALLADO_DE_VERDAD, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.friccion is True
    assert SIN_RESPUESTA in _errores(r)


def test_sin_silencio_medido_NO_hay_friccion_ni_texto():
    """RESUELTO DE RAIZ el 2026-08-14, mejor que con la rama por origen que hubo primero.

    El arreglo inicial partia el texto en dos segun quien hubiera encendido la friccion.
    Al retirarse `cliente_reinsistio` de la nota (ver tests/test_reinsistencia_llm.py), la
    rama alternativa quedo inalcanzable: `friccion` implica `reasked`, o sea silencio
    medido con timestamps, y entonces la frase es cierta POR CONSTRUCCION.

    Las 36 filas que decian "sin respuesta del operador" sobre operadores que habian
    contestado todo ya no se demotan en absoluto.
    """
    r = score_by_motivo(target_messages=ATENDIDO_SIEMPRE, thread_context="",
                        llm=FakeLLM(_resp()))
    assert r.friccion is False
    assert SIN_RESPUESTA not in _errores(r)


def test_el_texto_de_friccion_SOLO_aparece_con_silencio_medido():
    # El invariante que reemplaza a la rama por origen: si el texto esta, hubo reloj.
    con = score_by_motivo(target_messages=CALLADO_DE_VERDAD, thread_context="",
                          llm=FakeLLM(_resp()))
    sin = score_by_motivo(target_messages=ATENDIDO_SIEMPRE, thread_context="",
                          llm=FakeLLM(_resp()))
    assert SIN_RESPUESTA in _errores(con)
    assert SIN_RESPUESTA not in _errores(sin)


def test_el_texto_no_se_duplica():
    r = score_by_motivo(target_messages=CALLADO_DE_VERDAD, thread_context="",
                        llm=FakeLLM(_resp()))
    errores = _errores(r)
    assert len(errores) == len(set(errores))


# --- la nota no se toca ----------------------------------------------------------

def test_ahora_la_estrella_SI_distingue_el_silencio_medido():
    """Antes las dos sesiones sacaban la misma nota; el texto era lo unico que las separaba.

    Retirada `cliente_reinsistio`, la sesion donde el operador contesto TODO deja de estar
    demotada, y solo baja la que tiene silencio real. Que es lo que se queria desde el
    principio: la nota distingue, no solo el texto.
    """
    con_silencio = score_by_motivo(target_messages=CALLADO_DE_VERDAD, thread_context="",
                                   llm=FakeLLM(_resp()))
    sin_silencio = score_by_motivo(target_messages=ATENDIDO_SIEMPRE, thread_context="",
                                   llm=FakeLLM(_resp()))
    assert con_silencio.stars < sin_silencio.stars, \
        f"{con_silencio.stars} vs {sin_silencio.stars}"
