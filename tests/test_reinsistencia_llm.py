"""La reinsistencia que REPORTA EL LLM tambien tiene que poder bajar la nota.

`friccion` se calculaba solo con `client_reasked` (determinista, que exige silencio real
del operador medido con timestamps). El campo `cliente_reinsistio` que el propio modelo
devuelve en su salida estructurada se guardaba en `dimensions` y **no alimentaba nada**
que pudiera demotar: solo entraba en `confuso_corroborado`, que a su vez no hace nada
cuando la claridad es 'dudoso' -- el valor modal, y el que se asume por omision.

MEDIDO el 2026-08-13 sobre el rescore v13: **87 filas con `cliente_reinsistio=true` y
`friccion=false`, de las cuales 71 (81,6%) quedaron en 4 y 5 estrellas** (59 'buena' + 12
'excelente'). El caso que lo destapo es una fila de 5 estrellas cuyo propio rationale la
desmiente: "no ofreció una solución alternativa ni escaló el caso cuando el cliente
insistió en que ya llevaba 10 minutos esperando". Cinco estrellas significa el MEJOR
ESCENARIO del motivo, y el texto al lado describe lo contrario.

Por que las dos señales y no una: `client_reasked` ve el RELOJ (4+ mensajes con silencio
real) y es ciega al contenido; el LLM LEE y ve al cliente repitiendo el pedido con otras
palabras, que es insistir sin necesidad de una rafaga. Se suman con OR.

LO QUE NO CAMBIA: la proteccion determinista. Si el operador resolvio -confirmo o mando el
comprobante- la friccion no demota, igual que antes. Es la regla que ya declaraba el
codigo ("lo determinista gana"): un cliente que insiste sobre una transaccion que SI se
completo no convierte el trabajo en deficiente.
"""
from src.scorer import score_by_motivo


def _cli(body: str) -> dict:
    return {"from_me": False, "is_note": False, "body": body, "media_type": "chat"}


def _op(body: str) -> dict:
    return {"from_me": True, "is_note": False, "body": body, "media_type": None,
            "sent_from": "WEB"}


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp(**over) -> dict:
    """Hechos que derivan a 'buena' con motivo `problema` (siempre fall-through).

    SIN `created_at` a proposito: asi `client_reasked` no puede disparar (exige medir
    silencio) y el test aisla el aporte de `cliente_reinsistio`.
    """
    resp = {
        "motivo": "problema",
        "dimensions": {"resolucion": "ok", "iniciativa": "ok", "cortesia": "cordial",
                       "aciertos": [], "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "rating_rationale": "atendio el reclamo",
        "recomendacion": "",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# El operador contesta pero NO resuelve: nada de confirmacion ni comprobante.
SIN_RESOLVER = [
    _cli("Ya llevo esperando 10 minutos"),
    _op("Debemos esperar que los proveedores actualicen los resultados, no se preocupe"),
]
# El operador SI resolvio: la confirmacion determinista protege el piso.
RESUELTO = [
    _cli("Ya llevo esperando 10 minutos"),
    _op("Listo"),
]


def test_la_reinsistencia_del_LLM_demota_cuando_el_operador_no_resolvio():
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_sin_reinsistencia_la_nota_no_se_mueve():
    # EL GUARD del cambio: la señal solo actua cuando el modelo la reporta.
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=False)))
    assert r.rating_label == "buena" and r.stars == 4


def test_la_resolucion_determinista_sigue_protegiendo_el_piso():
    # "lo determinista gana": el cliente insistio pero la operacion se completo.
    r = score_by_motivo(target_messages=RESUELTO, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.rating_label == "buena" and r.stars == 4


def test_un_excelente_no_sobrevive_a_la_reinsistencia_sin_resolucion():
    # El caso real: 5 estrellas con un rationale que admite que el cliente insistio.
    r = score_by_motivo(
        target_messages=SIN_RESOLVER, thread_context="",
        llm=FakeLLM(_resp(cliente_reinsistio=True, hizo_accion_extra=True,
                          cortesia_destacada=True)))
    assert r.stars < 5


def test_la_baja_queda_marcada_como_ajuste_determinista():
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.rating_rationale.startswith("[ajuste determinista de hechos]")
