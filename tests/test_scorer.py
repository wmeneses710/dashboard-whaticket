"""Tests del orquestador de scoring v2 (score_by_motivo).

El LLM emite HECHOS concretos (atendio/extra/cortesia/maltrato) y el CODIGO deriva
la etiqueta (label_from_facts) y la estrella. Ademas hay overrides deterministas de
los hechos (senal dura le gana al modelo) y el guard de motivo por comprobante.
"""
import pytest

from src.scorer import ScoreResult, score_by_motivo

# Mensajes NEUTROS: no disparan ninguna senal determinista (ni confirmacion, ni
# media, ni push, ni maltrato) -> sirven para testear la derivacion PURA.
NEUTRAL = [
    {"from_me": False, "is_note": False, "body": "¿una consulta?"},
    {"from_me": True, "is_note": False, "body": "buenas, decime"},
]
# Deposito con confirmacion del agente (dispara operator_resolved).
MSGS = [
    {"from_me": False, "is_note": False, "body": "no me llego la recarga"},
    {"from_me": True, "is_note": False, "body": "ya te la acredito"},
]
# Con EMPUJE concreto del agente (link) -> operator_pushed=True. Necesario para que
# buena/excelente sobrevivan el cap de uplift (PIEZA 2).
PUSH = [
    {"from_me": False, "is_note": False, "body": "quiero el bono"},
    {"from_me": True, "is_note": False, "body": "Registrate acá https://www.sorti.ec/register y aprovechá"},
]


class FakeLLM:
    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def chat_json(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        return self.resp


def _motivo_resp(**over):
    """Salida base del LLM: hechos que derivan a 'buena' (atendio limpio, sin extra).

    En v4 el piso limpio vale 4: hacer bien el trabajo ya no necesita un empuje
    comercial encima para pasar de 3.
    """
    resp = {
        "motivo": "info",
        "dimensions": {
            "resolucion": "respondio la consulta",
            "iniciativa": "no ofrecio nada extra",
            "cortesia": "cordial",
            "errores": [],
        },
        "atendio_el_motivo": True,
        "hizo_accion_extra": False,
        "cortesia_destacada": False,
        "hubo_maltrato_grave": False,
        "rating_rationale": "respondio correctamente",
        "recomendacion": "podrias invitar a un deposito",
        "atencion": "pasivo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


# --- derivacion HECHOS -> etiqueta ----------------------------------------

def test_atendio_limpio_es_buena():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert isinstance(r, ScoreResult)
    assert r.motivo == "info"
    assert r.rating_label == "buena" and r.stars == 4
    assert r.llm_model == "qwen3.5:4b"


def test_no_atendio_es_deficiente():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(atendio_el_motivo=False)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_atendio_mas_extra_con_empuje_es_excelente():
    # v4: una capa por encima del trabajo limpio ya es el mejor escenario.
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True)))
    assert r.rating_label == "excelente" and r.stars == 5


def test_atendio_extra_y_cortesia_con_empuje_es_excelente():
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True, cortesia_destacada=True)))
    assert r.rating_label == "excelente" and r.stars == 5


# --- PIEZA 2: cap de uplift — AHORA SOLO EN `promo` ------------------------
# Decision del negocio del 2026-08-05, probada con datos: el uplift SOLO mueve la
# aguja en promo (deposito posterior 24,9% -> 34,1% con empuje+material, +9,2 pp),
# mientras que en retiro la EMPEORA (83,8% -> 69,9%). Aplicarlo a todos los motivos
# convertia al empuje comercial en un peaje: medido el 2026-08-06, tumbaba a 3
# estrellas al 47-67% de las sesiones segun el motivo, incluidas 135 de 149
# transacciones de deposito hechas perfectas (respuesta <=2 min + acreditacion
# confirmada). Un deposito bien atendido no necesita que le vendan un bono encima.

def test_cap_uplift_sigue_vivo_en_promo():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True,
                                                 cortesia_destacada=True)))
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.floor_applied is True


def test_cap_uplift_NO_aplica_fuera_de_promo():
    # Mismo caso exacto, cambiando solo el motivo: sin empuje comercial, un deposito
    # bien atendido conserva su nota.
    # `registro` NO entra en este loop: tiene su propio techo en el fall-through (PIEZA 3),
    # porque con mensajes NEUTROS no hay ninguna señal de que el alta se haya guiado.
    for motivo in ("deposito", "retiro", "soporte_cuenta", "info", "problema"):
        r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                            llm=FakeLLM(_motivo_resp(motivo=motivo, hizo_accion_extra=True,
                                                     cortesia_destacada=True)))
        assert r.rating_label == "excelente" and r.stars == 5, motivo


# --- PIEZA 3: techo de `registro` en el fall-through -----------------------
# Llegar al pase con LLM con motivo 'registro' PRUEBA que score_registro devolvio None,
# o sea que la sesion no fue una transaccion: el alta NO se cerro. Y el mejor escenario
# de la rubrica de registro es, textual, "cierra el alta y encamina el primer deposito"
# -> 'excelente' es inalcanzable en este camino POR CONSTRUCCION.
# Hallado el 2026-08-07 en la copia de prod: 3 de las 6 filas de registro salieron con 5
# estrellas y un rationale que las desmentia ("no guio paso a paso ni proporciono el link
# de registro"). El cliente pregunto como activar su cuenta y se quedo sin activarla.

def test_registro_sin_transaccion_no_llega_a_excelente():
    # Con empuje (mando el link): guio, pero el alta no se cerro -> "se hizo bien", 4.
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro", hizo_accion_extra=True,
                                                 cortesia_destacada=True)))
    assert r.rating_label == "buena" and r.stars == 4
    assert r.floor_applied is True


def test_registro_sin_transaccion_ni_empuje_topa_en_aceptable():
    # Reproduce la sesion 90a5a53a de prod: puro template ("animate y me avisas"), sin
    # link, sin datos, sin alta. El 'atendio' es solo palabra del modelo -> techo en 3.
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro", hizo_accion_extra=True,
                                                 cortesia_destacada=True)))
    assert r.rating_label == "aceptable" and r.stars == 3
    assert r.floor_applied is True


def test_registro_TRANSACCIONAL_no_lo_toca_el_techo():
    # El techo vive SOLO en el fall-through. Un alta cerrada (datos + credenciales +
    # deposito) sigue saliendo por src/registro.py, con su 5 intacto.
    _ts = lambda m: __import__("datetime").datetime(  # noqa: E731
        2026, 3, 10, 20, 0, tzinfo=__import__("datetime").timezone.utc
    ) + __import__("datetime").timedelta(minutes=m)
    tx = [
        {"created_at": _ts(0), "from_me": True, "is_note": False, "sent_from": "OPERATOR",
         "media_type": "chat", "body": "Ayudame con los datos para tu registro Nombre de usuario: Correo:"},
        {"created_at": _ts(1), "from_me": False, "is_note": False, "media_type": "chat",
         "body": "Nancy Toaquiza toaquizanancy68@gmail.com 0986987466"},
        {"created_at": _ts(3), "from_me": True, "is_note": False, "sent_from": "OPERATOR",
         "media_type": "chat", "body": "Estas son tus credenciales Usuario: nancy593 Clave: 12345"},
        {"created_at": _ts(10), "from_me": False, "is_note": False, "media_type": "chat",
         "body": "listo, ahi va mi recarga"},
        {"created_at": _ts(10), "from_me": False, "is_note": False, "media_type": "image",
         "body": ""},
    ]
    r = score_by_motivo(target_messages=tx, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro")))
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.llm_model == "determinista/registro-v1"


def test_registro_sin_transaccion_no_INVENTA_notas_peores():
    # El techo solo BAJA lo que estaba por encima; un 'deficiente' del modelo no se toca
    # (nada de castigar dos veces la misma sesion).
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro", atendio_el_motivo=False)))
    assert r.rating_label == "deficiente" and r.stars == 2


# --- deposito: la nota la manda la rubrica DETERMINISTA -------------------
# Cuando el LLM clasifica `deposito` y la sesion es una TRANSACCION (el cliente dio
# contexto de recarga y mando el comprobante), la nota sale de src/deposito.py: los
# tres hechos que la definen — el reloj, la acreditacion y el chequeo de cierre — son
# verificables. El LLM conserva su trabajo irremplazable: decir que motivo es.

_DEP_TX = [
    {"created_at": __import__("datetime").datetime(2026, 3, 10, 20, 0,
                                                   tzinfo=__import__("datetime").timezone.utc),
     "from_me": False, "is_note": False, "body": "les mando el comprobante de la recarga",
     "media_type": "chat"},
    {"created_at": __import__("datetime").datetime(2026, 3, 10, 20, 0,
                                                   tzinfo=__import__("datetime").timezone.utc),
     "from_me": False, "is_note": False, "body": "", "media_type": "image"},
    {"created_at": __import__("datetime").datetime(2026, 3, 10, 20, 1,
                                                   tzinfo=__import__("datetime").timezone.utc),
     "from_me": True, "is_note": False, "sent_from": "OPERATOR", "media_type": "chat",
     "body": "Estamos verificando tu comprobante. Tu recarga se reflejara en breve."},
    {"created_at": __import__("datetime").datetime(2026, 3, 10, 20, 3,
                                                   tzinfo=__import__("datetime").timezone.utc),
     "from_me": True, "is_note": False, "sent_from": "OPERATOR", "media_type": "chat",
     "body": "Gracias por tu recarga. Tu saldo ya esta disponible."},
]


def test_deposito_transaccion_usa_la_rubrica_determinista():
    # El LLM dice cortesia_destacada (que en v4 daria 5), pero la nota real es 4:
    # acuso rapido y acredito, pero no chequeo si faltaba algo.
    r = score_by_motivo(target_messages=_DEP_TX, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito",
                                                 cortesia_destacada=True)))
    assert r.motivo == "deposito"
    assert r.rating_label == "buena" and r.stars == 4
    assert r.llm_model == "determinista/deposito-v1"


def test_deposito_CONSULTA_sigue_por_el_pase_con_LLM():
    # Sin comprobante del cliente no hay transaccion: la rubrica determinista no
    # aplica y decide el pase normal.
    msgs = [{"from_me": False, "is_note": False, "body": "como hago para recargar?"},
            {"from_me": True, "is_note": False, "body": "por transferencia bancaria"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito")))
    assert r.llm_model == "qwen3.5:4b"


# --- retiro: la nota la manda la rubrica DETERMINISTA ---------------------

def _ts(minutos):
    import datetime as _dt
    return _dt.datetime(2026, 3, 10, 20, 0, tzinfo=_dt.timezone.utc) + _dt.timedelta(minutes=minutos)


_RET_TX = [
    {"created_at": _ts(0), "from_me": False, "is_note": False, "media_type": "chat",
     "body": "Monto a retirar: 30 Nombres: Alan Cedula: 0951964055"},
    {"created_at": _ts(1), "from_me": True, "is_note": False, "sent_from": "OPERATOR",
     "media_type": "chat", "body": "Tu retiro esta en proceso 🔄"},
    {"created_at": _ts(8), "from_me": True, "is_note": False, "sent_from": "OPERATOR",
     "media_type": "image", "body": ""},
]


def test_retiro_transaccion_usa_la_rubrica_determinista():
    r = score_by_motivo(target_messages=_RET_TX, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="retiro",
                                                 cortesia_destacada=True)))
    assert r.motivo == "retiro"
    assert r.rating_label == "buena" and r.stars == 4
    assert r.llm_model == "determinista/retiro-v1"


def test_retiro_CONSULTA_sigue_por_el_pase_con_LLM():
    msgs = [{"from_me": False, "is_note": False, "body": "como hago para retirar?"},
            {"from_me": True, "is_note": False, "body": "por transferencia"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="retiro")))
    assert r.llm_model == "qwen3.5:4b"


# --- PIEZA 1: piso del front-of-funnel (flujo de anuncio) -----------------

def test_piso_funnel_info_con_empuje_no_es_deficiente():
    # el LLM dice que NO atendió, pero el agente mandó link/promo (piso del flujo anuncio)
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info", atendio_el_motivo=False,
                                                 hizo_accion_extra=False, cortesia_destacada=False)))
    assert r.rating_label == "buena" and r.stars == 4
    assert r.floor_applied is True


def test_verifier_recupera_la_nota_en_borderline():
    # En promo (unico motivo con cap) sin señal fuerte se capearia, pero el
    # verificador confirma uplift genuino y la recupera.
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True)),
                        verifier=lambda msgs, motivo: True)
    assert r.rating_label == "excelente" and r.stars == 5


def test_verifier_falso_capea_a_aceptable_en_promo():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", hizo_accion_extra=True)),
                        verifier=lambda msgs, motivo: False)
    assert r.rating_label == "aceptable"


def test_recommender_pisa_la_recomendacion():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp()),
                        recommender=lambda msgs, motivo, label: "consejo del subagente")
    assert r.recomendacion == "consejo del subagente"


def test_recommender_que_falla_no_tumba_el_score():
    def boom(*a): raise RuntimeError("x")
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp()), recommender=boom)
    assert r.rating_label == "buena" and r.recomendacion == "podrias invitar a un deposito"


def test_info_sin_consulta_no_es_deficiente():
    # cliente solo agradeció (sin pregunta) y el agente respondió cordial -> piso limpio
    msgs = [{"from_me": False, "is_note": False, "body": "Gracias"},
            {"from_me": True, "is_note": False, "body": "Con gusto, cualquier cosa avisá"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info", atendio_el_motivo=False)))
    assert r.rating_label == "buena" and r.floor_applied is True


def test_info_con_consulta_evadida_sigue_deficiente():
    # el cliente SÍ preguntó y el agente no atendió (sin push/resolución) -> sigue deficiente
    msgs = [{"from_me": False, "is_note": False, "body": "¿cuál es el retiro mínimo?"},
            {"from_me": True, "is_note": False, "body": "hola buenas"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info", atendio_el_motivo=False)))
    assert r.rating_label == "deficiente"


def test_problema_no_se_floorea_por_empuje():
    # en 'problema' un empuje comercial NO es resolución -> sigue deficiente si el LLM dijo que no atendió
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema", atendio_el_motivo=False,
                                                 hizo_accion_extra=False, cortesia_destacada=False)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_deposit_observed_string_false_no_se_invierte():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(deposit_observed="false")))
    assert r.deposit_observed is False


def test_recomendacion_pasa_al_resultado():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert r.recomendacion == "podrias invitar a un deposito"


def test_recomendacion_se_augmenta_con_fragmento_determinista():
    # el agente entrega credenciales de alta manual -> el fragmento determinista
    # de cambio de contraseña debe anteponerse a la recomendacion del LLM.
    msgs = [
        {"from_me": False, "is_note": False, "body": "ya me registraron?"},
        {"from_me": True, "is_note": False, "body": "tu usuario es juan123 tu contraseña es abc456"},
    ]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="soporte_cuenta")))
    assert "cambie la contraseña" in r.recomendacion
    assert r.recomendacion.endswith("podrias invitar a un deposito")


# --- validacion de salida -------------------------------------------------

def test_rechaza_motivo_invalido():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="chacharacha")))


def test_rechaza_salida_sin_hechos_requeridos():
    with pytest.raises(ValueError):
        score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM({"motivo": "info", "rating_rationale": "x"}))


def test_atencion_ausente_degrada_a_none():
    resp = _motivo_resp()
    del resp["atencion"]
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(resp))
    assert r.atencion is None


def test_atencion_fuera_del_enum_degrada_a_none():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(atencion="mas_o_menos")))
    assert r.atencion is None


# --- guard de motivo por comprobante --------------------------------------

def test_deposit_hint_corrige_retiro_a_deposito():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="retiro")), deposit_hint=True)
    assert r.motivo == "deposito"


def test_deposit_hint_no_toca_otros_motivos():
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="info")), deposit_hint=True)
    assert r.motivo == "info"


def test_deposit_hint_corrige_problema_a_deposito_con_confirmacion():
    msgs = [{"from_me": False, "is_note": False, "body": "Abono 10 a deuda"},
            {"from_me": True, "is_note": False, "body": "ing"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema")), deposit_hint=True)
    assert r.motivo == "deposito"


def test_deposit_hint_no_toca_problema_sin_confirmacion():
    msgs = [{"from_me": False, "is_note": False, "body": "mandé comprobante y no me acreditan"},
            {"from_me": True, "is_note": False, "body": "déjame revisar con el área"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema")), deposit_hint=True)
    assert r.motivo == "problema"


# --- overrides deterministas de HECHOS ------------------------------------

def test_override_atendio_si_agente_confirmo_en_transaccional():
    # el LLM dice que NO atendio, pero el agente confirmo ("acredito") en un deposito
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito", atendio_el_motivo=False)))
    assert r.rating_label == "buena" and r.stars == 4
    assert r.floor_applied is True


def test_mala_sin_maltrato_detectado_no_cae_a_mala():
    # el LLM marca maltrato pero no hay insulto real -> se descarta -> no es 'mala'
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", atendio_el_motivo=False,
                                                 hubo_maltrato_grave=True)))
    assert r.rating_label == "deficiente" and r.floor_applied is True


def test_mala_con_maltrato_detectado_se_respeta():
    msgs = [{"from_me": False, "is_note": False, "body": "ayuda"},
            {"from_me": True, "is_note": False, "body": "no seas tonto, ya te dije"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="problema", hubo_maltrato_grave=True)))
    assert r.rating_label == "mala" and r.stars == 1


# --- atencion #5 ----------------------------------------------------------

def test_atencion_empujo_si_agente_manda_link():
    msgs = [{"from_me": False, "is_note": False, "body": "cómo me registro"},
            {"from_me": True, "is_note": False, "body": "Regístrate acá https://www.sorti.ec/register"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="registro", atencion="pasivo")))
    assert r.atencion == "empujo"


def test_pasa_deposit_hint_al_prompt():
    llm = FakeLLM(_motivo_resp())
    score_by_motivo(target_messages=MSGS, thread_context="", llm=llm, deposit_hint=True)
    assert "HINT DETERMINISTA" in llm.calls[0][0]


# --- Modulador claridad + fricción (v3) -----------------------------------

# Cliente reinsiste sin respuesta y el agente NO resuelve (deflexión) -> fricción.
REASK = [
    {"from_me": False, "is_note": False, "body": "hice un deposito"},
    {"from_me": False, "is_note": False, "body": "ayuda"},
    {"from_me": False, "is_note": False, "body": "?"},
    {"from_me": False, "is_note": False, "body": "?"},
    {"from_me": True, "is_note": False, "body": "comuníquese con su agente al 099"},
]


def test_confuso_sin_resolucion_baja_a_deficiente():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(claridad="confuso")))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_confuso_no_baja_si_el_agente_resolvio_determinista():
    # MSGS tiene "acredito" -> operator_resolved=True protege del confuso difuso del LLM
    r = score_by_motivo(target_messages=MSGS, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito", claridad="confuso")))
    assert r.rating_label == "buena" and r.stars == 4


def test_confuso_corroborado_por_reinsistencia_llm_baja_a_deficiente():
    # sin fricción determinista, pero el LLM reporta que el cliente reinsistió
    # (cliente_reinsistio) -> corrobora el confuso -> deficiente.
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="",
                        llm=FakeLLM(_motivo_resp(claridad="confuso", cliente_reinsistio=True)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_gate1_confuso_sin_pregunta_ni_reinsistencia_se_neutraliza():
    # el cliente no preguntó nada ni reinsistió (no había nada que aclarar): un
    # 'confuso' del LLM se neutraliza a 'dudoso' -> no hunde la nota.
    msgs = [{"from_me": False, "is_note": False, "body": "todo bien, gracias"},
            {"from_me": True, "is_note": False, "body": "genial, que tengas buen dia"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(claridad="confuso")))
    assert r.rating_label == "buena"


def test_gate2_confuso_con_pregunta_y_empuje_concreto_no_corroborado_se_rescata():
    # el cliente preguntó y el agente mandó un empuje concreto (link) sin que el
    # cliente reinsistiera: el confuso NO queda corroborado -> se rescata a
    # 'aceptable' (no hunde a deficiente) y no marca el override determinista.
    msgs = [{"from_me": False, "is_note": False, "body": "¿cómo puedo depositar?"},
            {"from_me": True, "is_note": False, "body": "Depositá acá https://www.sorti.ec/deposit"}]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_motivo_resp(claridad="confuso")))
    assert r.rating_label == "aceptable"
    assert r.floor_applied is False


def test_friccion_determinista_baja_a_deficiente():
    r = score_by_motivo(target_messages=REASK, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito", atendio_el_motivo=True)))
    assert r.rating_label == "deficiente" and r.stars == 2


def test_ghosteo_total_no_atendio_con_friccion_es_mala():
    r = score_by_motivo(target_messages=REASK, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="deposito", atendio_el_motivo=False)))
    assert r.rating_label == "mala" and r.stars == 1


def test_claridad_ausente_es_neutral_no_demota():
    r = score_by_motivo(target_messages=NEUTRAL, thread_context="", llm=FakeLLM(_motivo_resp()))
    assert r.rating_label == "buena"


def test_score_expone_aciertos_y_claridad():
    r = score_by_motivo(target_messages=PUSH, thread_context="",
                        llm=FakeLLM(_motivo_resp(motivo="promo", claridad="claro",
                                                 hizo_accion_extra=True, cortesia_destacada=True)))
    claves = [a["clave"] for a in r.aciertos]
    assert "iniciativa" in claves and "cortesia" in claves
    assert r.claridad in ("claro", "confuso", "dudoso")
