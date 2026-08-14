"""La cortesia NO compra el mejor escenario. Hace falta una ACCION verificable.

`label_from_facts` subia a 'excelente' con `hizo_accion_extra` **o** `cortesia_destacada`,
sin exigir ninguna de las dos. Y `cortesia_destacada` es casi gratis: los operadores usan
plantillas calidas por defecto -- hay 212 plantillas globales con mas de 300 usos cada una,
la mas repetida con 79.447.

ESTO YA HABIA PASADO Y ESTA DOCUMENTADO. El docstring de `src/deposito.py` explica que la
escala vieja se rompio EXACTAMENTE asi: **el 47,5% de los depositos llegaba a 5 SOLO por
cortesia**. Las rubricas deterministas se rehicieron para arreglarlo (el unico disparador
del 5 en `deposito` es `algo_mas`, no el tono), pero el camino LLM -- el fall-through de las
sesiones sin transaccion detectada -- siguio con la regla vieja.

MEDIDO el 2026-08-14 sobre v15, camino LLM (aciertos sin la clave `iniciativa`):

    motivo      excelentes   con iniciativa   SOLO cortesia
    registro           142              112       30  (21%)
    deposito            64               24       40  (63%)
    problema            48               12       36  (75%)
    retiro              30                6       24  (80%)
    -----------------------------------------------------
    total              284              154      130  (46%)

**Casi la mitad de los 'excelente' del camino LLM se compran solo con tono.**

POR QUE NO ES CAMBIAR UN CAMPO DEL LLM POR OTRO. `hizo_accion_extra` describe una ACCION
("le mostro con una captura", "le hizo el seguimiento"), verificable leyendo el transcript;
`cortesia_destacada` describe el TONO, que la plantilla ya trae puesto.

LA CORTESIA NO DESAPARECE: sigue produciendo su acierto en `aciertos[]`, o sea que se
reconoce como una fortaleza. Lo que deja de hacer es comprar la nota maxima.
"""
from src.rubrics import label_from_facts
from src.scorer import score_by_motivo

NEUTRAL = [
    {"from_me": False, "is_note": False, "body": "una consulta"},
    {"from_me": True, "is_note": False, "body": "claro, te cuento"},
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
        "dimensions": {"resolucion": "atendio", "iniciativa": "nada extra",
                       "cortesia": "muy calido", "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": True,
        "hubo_maltrato_grave": False,
        "cliente_reinsistio": False,
        "rating_rationale": "atendio con mucha calidez",
        "recomendacion": "",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


def _hechos(**over):
    base = dict(atendio_motivo=True, hizo_accion_extra=False, cortesia_destacada=False,
                hubo_maltrato_grave=False, claridad="claro", friccion=False,
                confuso_corroborado=False)
    base.update(over)
    return base


# --- la derivacion pura ----------------------------------------------------------

def test_la_cortesia_sola_ya_NO_es_excelente():
    assert label_from_facts(**_hechos(cortesia_destacada=True)) == "buena"


def test_la_accion_extra_sola_SIGUE_siendo_excelente():
    assert label_from_facts(**_hechos(hizo_accion_extra=True)) == "excelente"


def test_accion_extra_mas_cortesia_sigue_siendo_excelente():
    assert label_from_facts(
        **_hechos(hizo_accion_extra=True, cortesia_destacada=True)) == "excelente"


def test_sin_ninguna_de_las_dos_sigue_siendo_buena():
    assert label_from_facts(**_hechos()) == "buena"


def test_la_cortesia_no_rescata_una_nota_hundida():
    # No cambia nada de lo que ya demotaba: el piso manda igual que antes.
    assert label_from_facts(**_hechos(cortesia_destacada=True, friccion=True)) == "deficiente"
    assert label_from_facts(
        **_hechos(cortesia_destacada=True, atendio_motivo=False)) == "deficiente"


# --- integrado, y la cortesia sigue VISIBLE --------------------------------------

def test_en_el_scorer_la_cortesia_sola_da_cuatro_no_cinco():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_resp()))
    assert r.rating_label == "buena" and r.stars == 4


def test_la_cortesia_se_SIGUE_reconociendo_como_acierto():
    # Se deja de premiar con la nota maxima, no de reconocer.
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_resp()))
    claves = {a.get("clave") for a in (r.dimensions or {}).get("aciertos") or []}
    assert "cortesia" in claves


def test_con_accion_extra_el_scorer_sigue_dando_cinco():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_resp(hizo_accion_extra=True)))
    assert r.stars == 5
