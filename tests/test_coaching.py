"""Tests del COACHING: el unico campo que una PERSONA lee y que le cambia el comportamiento.

Una nota mal puesta es injusta; un consejo mal puesto enseña algo equivocado. Por eso el
coaching tiene sus propias invariantes, distintas de las de la nota:

  1. LOS FRAGMENTOS DE NEGOCIO llegan a TODOS los caminos, no solo al del LLM.
  2. `agente` NUNCA recibe coaching comercial: es un revendedor operando una caja, no un
     jugador a convertir. Un consejo de promo/bono/registro ahi no tiene sentido.
  3. El 5 no lleva consejo (no hay nada que corregir).
  4. Cada rama nombra SU causa: el 2 de deposito por no acreditar habla de confirmar, y el
     2 por tardar habla de tiempo. Es la propiedad que protege `a0d17f1` ("que el consejo
     hable de la rama"): un consejo que nombra otro alcance es imposible de verificar
     leyendo el chat.
  5. NO recomendar lo que el operador YA HIZO.

Todo PURO: sin LLM y sin BD.
"""
from datetime import datetime, timedelta, timezone

from src.agilidad import _COACHING as COACHING_AGILIDAD
from src.deposito import score_deposito
from src.info import score_info
from src.promo import score_promo
from src.registro import score_registro
from src.retiro import score_retiro
from src.scorer import score_by_motivo
from src.soporte import score_soporte

BASE = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)   # 10:00 local, en horario

DATOS = "Nancy Toaquiza toaquizanancy68@gmail.com 0986987466"
CREDENCIALES = "Estas son tus credenciales Usuario: nancy593 Clave: 12345"


def _cli(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": False,
            "is_note": False, "body": body, "media_type": media}


def _op(seg, body="", media="chat"):
    return {"created_at": BASE + timedelta(seconds=seg), "from_me": True,
            "is_note": False, "body": body, "media_type": media,
            "sent_from": "OPERATOR"}


# Vocabulario COMERCIAL: lo que se le dice a un JUGADOR para convertirlo. Nada de esto tiene
# sentido para un `agente`, que ya es cliente y opera una caja.
_COMERCIAL = ("promo", "bono", "registr", "recarga tu", "deposita", "apost", "afiliado",
              "invitalo", "invítalo", "convert")


# --- 2. el AGENTE no recibe coaching comercial -----------------------------------
def test_el_coaching_de_agilidad_no_habla_de_conversion():
    for label, texto in COACHING_AGILIDAD.items():
        bajo = texto.lower()
        for palabra in _COMERCIAL:
            assert palabra not in bajo, f"agilidad[{label}] dice {palabra!r}: {texto}"


def test_el_coaching_de_agilidad_habla_de_LA_CAJA():
    # Lo que SI corresponde: velocidad de respuesta al pedido del agente.
    for label, texto in COACHING_AGILIDAD.items():
        bajo = texto.lower()
        assert any(p in bajo for p in ("responder", "respuesta", "contestar", "esperando",
                                       "objetivo", "avisando")), f"{label}: {texto}"


# --- 3. el 5 no lleva consejo -----------------------------------------------------
def test_el_cinco_no_lleva_consejo():
    # deposito 5: acuso rapido, acredito y pregunto si faltaba algo.
    s = score_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(20, "recibido, ya lo cargo"), _op(60, "listo, tu saldo ya está disponible"),
        _op(90, "¿Hay algo más en lo que te pueda ayudar?"),
    ], BASE + timedelta(seconds=600))
    assert s is not None and s.stars == 5, s.rating_rationale
    assert s.recomendacion == "", s.recomendacion


# --- 4. cada rama nombra SU causa -------------------------------------------------
def test_deposito_sin_acreditar_habla_de_CONFIRMAR_no_de_tiempo():
    s = score_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(20, "recibido, en breve tendrás tu saldo"),
    ], BASE + timedelta(seconds=600))
    assert s is not None and s.stars == 2, s.rating_rationale
    bajo = s.recomendacion.lower()
    assert "confirm" in bajo, s.recomendacion
    # NO puede reprocharle el tiempo: avisó en 20 segundos.
    assert "tard" not in bajo, s.recomendacion


def test_deposito_que_tardo_habla_de_TIEMPO_no_de_confirmar():
    s = score_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(600, "recibido"), _op(700, "listo, tu saldo ya está disponible"),
    ], BASE + timedelta(seconds=1200))
    assert s is not None
    bajo = s.recomendacion.lower()
    assert any(p in bajo for p in ("tard", "aviso", "apenas", "acusar")), \
        s.recomendacion


def test_retiro_sin_comprobante_habla_del_COMPROBANTE():
    s = score_retiro([
        _cli(0, "Monto a retirar: 30 Cedula: 0951964055 Banco: Guayaquil"),
        _op(30, "Tu retiro está en proceso"),
    ], BASE + timedelta(seconds=600))
    assert s is not None and s.stars == 2, s.rating_rationale
    assert "comprobante" in s.recomendacion.lower(), s.recomendacion


def test_cada_motivo_da_un_consejo_de_SU_dominio():
    # El consejo tiene que hablar del tramite de ese motivo, no de otro.
    casos = [
        (score_promo([_cli(0, "que promos tienen?"), _op(400, "el bono del 100%")]),
         ("promo", "capt", "texto", "video")),
        (score_info([_cli(0, "cuanto cobran de comision?"), _op(400, "no cobramos")],
                    BASE + timedelta(seconds=900)), ("respond", "consulta", "duda", "minuto")),
        (score_soporte([_cli(0, "no puedo entrar, clave incorrecta"),
                        _op(400, "te la reseteo, cambiala al ingresar")],
                       BASE + timedelta(seconds=900)),
         ("soporte", "trabad", "paso", "escal", "tard")),
    ]
    for s, esperadas in casos:
        assert s is not None, "la rubrica no califico el caso"
        if s.recomendacion:
            bajo = s.recomendacion.lower()
            assert any(p in bajo for p in esperadas), f"{s.rubric}: {s.recomendacion}"


# --- 5. no recomendar lo YA HECHO -------------------------------------------------
def test_no_le_pide_preguntar_algo_mas_si_YA_pregunto():
    s = score_deposito([
        _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
        _op(200, "recibido"), _op(260, "listo, tu saldo ya está disponible"),
        _op(300, "¿Hay algo más en lo que te pueda ayudar?"),
    ], BASE + timedelta(seconds=900))
    assert s is not None
    assert "algo más" not in s.recomendacion.lower(), s.recomendacion


# --- 1. los FRAGMENTOS DE NEGOCIO llegan al camino determinista -------------------
# `refine_recomendacion` se llamaba SOLO en scorer.py (el camino LLM), y las rubricas
# deterministas devuelven su propio ScoreResult sin pasar por ahi. Resultado MEDIDO el
# 2026-08-12: de las 294 filas de `determinista/registro-v1`, **278 entregaron credenciales
# y NI UNA le dice al cliente que cambie la contraseña**. En el camino LLM son 88 de 397
# (22%). Es una regla de SEGURIDAD que existe en el codigo (`_FRAG_PASSWORD`) y no disparaba
# justo donde mas aplica, porque `registro` es determinista cuando SI hubo alta.

# Se entra por `score_by_motivo`, que es el camino REAL de produccion (worker.py ->
# score_by_motivo -> la rubrica determinista). Llamar a la rubrica directo no ejercita los
# fragmentos, y era el error de la primera version de este test.

class _FakeLLM:
    """Devuelve el motivo pedido; los fragmentos no dependen de los hechos del modelo."""

    model = "qwen3:14b"

    def __init__(self, motivo):
        self.motivo = motivo

    def chat_json(self, system, user, schema=None):
        return {
            "motivo": self.motivo,
            "dimensions": {"resolucion": "x", "iniciativa": "x", "cortesia": "x",
                           "errores": []},
            "atendio_el_motivo": True, "hizo_accion_extra": False,
            "cortesia_destacada": False, "hubo_maltrato_grave": False,
            "claridad": "claro", "cliente_reinsistio": False,
            "rating_rationale": "x", "recomendacion": "", "atencion": "pasivo",
            "deposit_observed": False,
        }


def test_el_fragmento_de_contrasena_llega_cuando_el_operador_creo_la_cuenta():
    s = score_by_motivo(target_messages=[_cli(0, DATOS), _op(120, CREDENCIALES)],
                        thread_context="", llm=_FakeLLM("registro"))
    assert s is not None and s.llm_model.startswith("determinista"), s.llm_model
    assert "contrase" in s.recomendacion.lower(), s.recomendacion


def test_el_fragmento_NO_aparece_si_no_hubo_credenciales():
    s = score_by_motivo(target_messages=[_cli(0, DATOS), _op(120, "ahi te reviso")],
                        thread_context="", llm=_FakeLLM("registro"))
    assert s is not None
    assert "contrase" not in s.recomendacion.lower(), s.recomendacion


def test_el_consejo_propio_de_la_rubrica_NO_se_pierde_al_agregar_el_fragmento():
    # El fragmento se AGREGA; el consejo de la rama tiene que seguir ahi.
    s = score_by_motivo(target_messages=[_cli(0, DATOS), _op(120, CREDENCIALES)],
                        thread_context="", llm=_FakeLLM("registro"))
    assert "primera recarga" in s.recomendacion.lower(), s.recomendacion


# --- 6. DICCION Y VALOR AGREGADO --------------------------------------------------
# MEDIDO en produccion el 2026-08-12: **824 de 1.108** recomendaciones deterministas usaban
# voseo rioplatense (`Confirmale`, `Mandale`, `preguntale`, `decile`, `avisale`) contra 1 de
# 462 del camino LLM, cuyo prompt dice "PROHIBIDO el voseo". Los operadores son ECUATORIANOS:
# el 74% del coaching que leen esta en un registro que no es el suyo, y las dos vias le hablan
# distinto. El acuerdo del proyecto para artefactos en español es NEUTRO/profesional.
# Y la REDUNDANCIA: 231 recomendaciones repetian "2 minutos" y 215 "algo mas", frases que ya
# estaban en el rationale. El rationale dice QUE paso; la recomendacion tiene que decir COMO
# hacerlo distinto. Si solo repite el reproche, no agrega nada.

_IMPERATIVOS_VOSEO = ("confirmale", "mandale", "preguntale", "decile", "avisale", "contale",
                      "fijate", "acordate", "mostrale", "pedile", "escribile", "dale ")


def _todos_los_textos():
    from src.agilidad import _COACHING as A
    from src.deposito import (_COACHING as D, _COACHING_1 as D1,
                              _COACHING_2_SIN_ACREDITAR as D2A, _COACHING_2_TARDE as D2T)
    from src.info import _COACHING as I, _COACHING_1 as I1
    from src.promo import _COACHING as P, _COACHING_1 as P1
    from src.registro import _COACHING as R, _COACHING_1 as R1
    from src.retiro import (_COACHING as T, _COACHING_1 as T1,
                            _COACHING_2_SIN_COMPROBANTE as T2S, _COACHING_2_TARDE as T2T)
    from src.soporte import (_COACHING as S, _COACHING_1 as S1,
                             _COACHING_2_LENTO as S2L, _COACHING_2_SIN_INTENTO as S2I)
    out = []
    for d in (A, D, I, P, R, T, S):
        out += [(f"{k}", v) for k, v in d.items()]
    for nombre, v in (("dep_1", D1), ("dep_2_sin", D2A), ("dep_2_tarde", D2T),
                      ("info_1", I1), ("promo_1", P1), ("reg_1", R1),
                      ("ret_1", T1), ("ret_2_sin", T2S), ("ret_2_tarde", T2T),
                      ("sop_1", S1), ("sop_2_lento", S2L), ("sop_2_sin", S2I)):
        out.append((nombre, v))
    return out


def test_el_coaching_no_usa_voseo():
    for clave, texto in _todos_los_textos():
        bajo = texto.lower()
        for v in _IMPERATIVOS_VOSEO:
            assert v not in bajo, f"{clave} usa voseo {v!r}: {texto}"


def test_el_coaching_dice_COMO_no_solo_QUE_paso():
    # Tiene que traer una accion o un criterio accionable, no solo el reproche. Se acepta un
    # imperativo neutro, un infinitivo de instruccion, o una formula de recomendacion.
    señales = ("confírm", "mánda", "pregúnt", "avísa", "dile", "muéstra", "pide", "envía",
               "conviene", "alcanza", "basta", "se apunta", "objetivo", "recuerda", "avisar",
               "cerrar con", "un primer mensaje", "una línea", "una imagen", "un video",
               "indícale", "acompañ", "responder", "contestar", "enviarlo", "enviar", "decirle")
    for clave, texto in _todos_los_textos():
        bajo = texto.lower()
        assert any(s in bajo for s in señales), f"{clave} no dice COMO: {texto}"
