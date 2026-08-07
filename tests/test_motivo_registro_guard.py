"""Si en la sesion SE CREO LA CUENTA, el motivo es `registro`. Decision del negocio.

MEDIDO el 2026-08-07 sobre 1.826 sesiones evaluadas: de las **163 altas consumadas** (el
cliente entrego sus datos personales Y el operador devolvio usuario y clave), solo 103
(63,2%) quedaron como `registro`. **40 (24,5%) quedaron como `promo`**, 12 como
soporte_cuenta y 2 como deposito.

Eso no es un detalle de etiqueta: esas 40 se calificaban con la rubrica de PROMO, que mide
si mando el flyer y empujo la conversion, cuando el hecho de la sesion fue una CUENTA
CREADA. La rubrica de registro mide otra cosa — el reloj entre el traspaso de datos y la
entrega de credenciales. Estaban evaluadas en el eje equivocado, y todo el trabajo sobre
`registro` no las tocaba porque nunca entraban por ahi.

Regla del negocio: "si en una se crea la cuenta es registro independientemente de lo que
haya antes o despues" — la promo fue el gancho, el alta es el hecho consumado. Es el mismo
criterio que ya regia para el deposito dentro de registro (la rubrica da 5 cuando el alta
cierra Y hay recarga en la misma sesion, sin que el motivo pase a ser `deposito`).
"""
from datetime import datetime, timedelta, timezone

from src.registro import se_creo_la_cuenta
from src.scorer import score_by_motivo

BASE = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)
DATOS = "Ana Rios ana@mail.com 0991234567"
CREDS = "Listo, tu usuario es anarios y la clave 12345"


def _cli(seg, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(seg, body, media="chat"):
    return {"created_at": BASE + timedelta(minutes=seg), "from_me": True,
            "is_note": False, "body": body, "sent_from": "OPERATOR", "media_type": media}


ALTA = [_cli(0, "vi la promo del bono de $5"),
        _op(1, "te cuento, con tu primera recarga tenes una freebet. Pasame tus datos"),
        _cli(2, DATOS),
        _op(3, CREDS)]


class FakeLLM:
    model = "qwen3:14b"

    def __init__(self, motivo):
        self.motivo = motivo

    def chat_json(self, system, user, schema=None):
        return {
            "motivo": self.motivo,
            "dimensions": {"resolucion": "atendio", "iniciativa": "ofrecio", "cortesia": "cordial",
                           "errores": []},
            "atendio_el_motivo": True, "hizo_accion_extra": True,
            "cortesia_destacada": False, "hubo_maltrato_grave": False,
            "claridad": "claro", "cliente_reinsistio": False,
            "rating_rationale": "x", "recomendacion": "", "atencion": "empujo",
            "deposit_observed": False,
        }


def test_se_creo_la_cuenta_detecta_el_alta():
    assert se_creo_la_cuenta(ALTA) is True


def test_pedir_datos_sin_entregar_credenciales_no_es_alta_cerrada():
    assert se_creo_la_cuenta(ALTA[:3]) is False


def test_credenciales_sin_datos_del_cliente_NO_es_alta_nueva():
    # Es un RESETEO de contraseña: el operador manda credenciales nuevas de una cuenta que
    # ya existia. Medidas 30 sesiones asi; ahi `soporte_cuenta` esta bien puesto y el guard
    # NO debe pisarlo.
    msgs = [_cli(0, "perdi mi clave"), _op(1, CREDS)]
    assert se_creo_la_cuenta(msgs) is False


def test_el_guard_corrige_promo_a_registro():
    # El caso de las 40: arranco por la promo y termino en un alta cerrada.
    r = score_by_motivo(target_messages=ALTA, thread_context="", llm=FakeLLM("promo"))
    assert r.motivo == "registro"
    # y ademas sale por la rubrica DETERMINISTA de registro, que es la vara correcta
    assert r.llm_model == "determinista/registro-v1"


def test_el_guard_corrige_soporte_a_registro():
    r = score_by_motivo(target_messages=ALTA, thread_context="", llm=FakeLLM("soporte_cuenta"))
    assert r.motivo == "registro"


def test_sin_alta_cerrada_el_guard_no_toca_el_motivo():
    msgs = [_cli(0, "vi la promo del bono"), _op(1, "te cuento como funciona el bono")]
    r = score_by_motivo(target_messages=msgs, thread_context="", llm=FakeLLM("promo"))
    assert r.motivo == "promo"


def test_un_reseteo_sigue_siendo_soporte():
    msgs = [_cli(0, "perdi mi clave"), _op(1, CREDS)]
    r = score_by_motivo(target_messages=msgs, thread_context="", llm=FakeLLM("soporte_cuenta"))
    assert r.motivo == "soporte_cuenta"
