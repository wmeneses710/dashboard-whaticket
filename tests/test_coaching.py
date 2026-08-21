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
import re

from src.catalogo_atc import RESPUESTAS_RAPIDAS
from datetime import datetime, timedelta, timezone

# Los textos de `agilidad` se mudaron a src/catalogo_coaching.py el 2026-08-21 (catalogo
# cerrado con codigo, para poder contarlos). El test sigue mirando los MISMOS textos, ahora
# desde su unica fuente de verdad.
from src.catalogo_coaching import CONSEJOS

COACHING_AGILIDAD = {c.situacion: c.texto for c in CONSEJOS if c.rubrica == "agilidad"}
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


# AGUJERO CERRADO el 2026-08-13. El test de arriba llama a `score_deposito` DIRECTO y nunca
# ejercita el camino real de produccion (`score_by_motivo` -> `refine_recomendacion`), que es
# donde el invariante se rompia: MEDIDO sobre el rescore v13, **623 de 4.782 filas en 5
# estrellas (13,0%) traian consejo correctivo**, y eran el 100% de los 5 del camino LLM
# (439/439) y el 100% de los 5 de `registro` determinista (155/155). Con los 13 invariantes
# en verde. Un test que no corre el camino de produccion no protege nada.
class _FakeLLMRaw:
    """Devuelve la salida CRUDA que se le pasa (hechos completos, no solo el motivo)."""

    model = "qwen3.5:4b"

    def __init__(self, resp):
        self.resp = resp

    def chat_json(self, system, user, schema=None):
        return self.resp


def _resp_excelente(**over):
    """Hechos que derivan a 'excelente' y un consejo correctivo del modelo."""
    resp = {
        "motivo": "problema",
        "dimensions": {"resolucion": "ok", "iniciativa": "ok", "cortesia": "cordial",
                       "aciertos": [], "errores": []},
        "atendio_el_motivo": True,
        "hizo_accion_extra": True,
        "cortesia_destacada": True,
        "hubo_maltrato_grave": False,
        "rating_rationale": "resolvió el reclamo enseguida",
        "recomendacion": "Conviene explicar mejor el siguiente paso al cliente.",
        "atencion": "empujo",
        "deposit_observed": False,
    }
    resp.update(over)
    return resp


def test_el_cinco_no_lleva_consejo_TAMBIEN_por_el_camino_real():
    msgs = [_cli(0, "no me aparece el bono"), _op(30, "ya lo revisé, quedó habilitado")]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=_FakeLLMRaw(_resp_excelente()))
    assert r.rating_label == "excelente" and r.stars == 5
    assert r.recomendacion == "", r.recomendacion


def test_el_cinco_SI_conserva_el_fragmento_de_SEGURIDAD():
    # LA EXCEPCION, y es deliberada: el fragmento de la contraseña no es un reproche al
    # operador, es una instruccion para el CLIENTE. Se agrego al camino determinista el
    # 2026-08-12 justamente porque no disparaba donde mas aplica (278 de 294 filas de
    # `registro` entregaron credenciales y NI UNA lo decia). El invariante del 5 saca el
    # consejo CORRECTIVO; no saca una regla de seguridad.
    msgs = [_cli(0, "quiero registrarme"), _op(30, CREDENCIALES)]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=_FakeLLMRaw(_resp_excelente()))
    assert r.stars == 5
    assert "contraseña" in r.recomendacion
    assert "siguiente paso" not in r.recomendacion, "quedó el correctivo del modelo"


def test_por_debajo_del_cinco_el_consejo_del_modelo_sobrevive():
    msgs = [_cli(0, "no me aparece el bono"), _op(30, "ya lo revisé, quedó habilitado")]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=_FakeLLMRaw(_resp_excelente(hizo_accion_extra=False,
                                                        cortesia_destacada=False)))
    assert r.stars == 4
    assert "siguiente paso" in r.recomendacion


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
    # El marcador cambio el 2026-08-13 con la reescritura del texto de `registro` 4: era
    # "primera recarga", que es justo la frase que el consejo compartia con el reproche del
    # rationale (ver test_el_consejo_no_es_la_PARAFRASIS_del_reproche). El INVARIANTE es el
    # mismo -- el consejo de la rama no se pierde al anteponerse el fragmento --, solo se
    # apunta a una frase del texto nuevo.
    s = score_by_motivo(target_messages=[_cli(0, DATOS), _op(120, CREDENCIALES)],
                        thread_context="", llm=_FakeLLM("registro"))
    assert "medios de pago" in s.recomendacion.lower(), s.recomendacion


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
    from src.catalogo_coaching import CONSEJOS as _C_A
    A = {c.situacion: c.texto for c in _C_A if c.rubrica == 'agilidad'}
    # LAS SIETE RUBRICAS VIVEN EN src/catalogo_coaching.py desde el 2026-08-21. El
    # inventario sale de ahi: una sola fuente, y las rubricas que se agreguen entran solas.
    from src.catalogo_coaching import CONSEJOS, FRAGMENTOS

    out = [(f"{c.rubrica}/{c.situacion}", c.texto) for c in CONSEJOS]
    out += [(f.codigo, f.texto) for f in FRAGMENTOS]
    return out


def _inventario_viejo_sin_usar():
    out = []
    for d in (A, D, I, P, R, T, S):
        out += [(f"{k}", v) for k, v in d.items()]
    for nombre, v in (("dep_1", D1), ("dep_2_sin", D2A), ("dep_2_tarde", D2T),
                      ("info_1", I1), ("promo_1", P1), ("reg_1", R1),
                      ("ret_1", T1), ("ret_2_sin", T2S), ("ret_2_tarde", T2T),
                      ("sop_1", S1), ("sop_2_sin", S2I)):
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
    # SE SACO "acompañ" el 2026-08-13: era el AGUJERO de este test. Esa palabra es la que usa
    # el REPROCHE del rationale de `registro` 4 ("No llegó a acompañarlo hasta la primera
    # recarga"), asi que aceptarla como prueba de que el texto "dice como" dejaba pasar un
    # consejo que era la paraphrasis del reproche. Una palabra reciclada del reproche no es
    # una instruccion.
    señales = ("confírm", "mánda", "pregúnt", "avísa", "dile", "muéstra", "pide", "envía",
               "conviene", "alcanza", "basta", "se apunta", "objetivo", "recuerda", "avisar",
               "cerrar con", "un primer mensaje", "una línea", "una imagen", "un video",
               "indícale", "indicarle", "responder", "contestar", "enviarlo", "enviar",
               "decirle", "pasar", "pasarle", "acusar", "verificá", "verificar",
               "guía", "guia", "suma ", "asegurarte", "crear")
    # `indicarle` y las otras entraron el 2026-08-21 y NO por relajar el invariante: al
    # migrar al catalogo, el inventario paso de 12 textos sueltos a los 35 completos. Las
    # ramas de derivacion y rechazo de deposito/registro nunca habian pasado por aca --
    # el inventario viejo listaba solo dep_1, dep_2_sin y dep_2_tarde-- y usan el
    # INFINITIVO ("Suma indicarle tambien que puede recargar...") donde la lista solo
    # tenia el imperativo. La forma verbal cambia; que el consejo diga COMO, no.
    for clave, texto in _todos_los_textos():
        bajo = texto.lower()
        # NOMBRAR UNA RESPUESTA RAPIDA DEL MANUAL ES LA FORMA MAS ACCIONABLE DE DECIR COMO,
        # y por eso vale sola. "/R2verificaciondeboleta apenas entra el comprobante" no deja
        # nada que interpretar: el operador la tiene en Whaticket y sabe cual es. Se exige
        # que este EN EL CATALOGO -- si no, seria una plantilla inventada, que es justo el
        # error critico E10 del manual ("Alterar respuestas rapidas... o informacion oficial").
        if any(rr.lower() in bajo for rr in RESPUESTAS_RAPIDAS):
            continue
        assert any(s in bajo for s in señales), f"{clave} no dice COMO: {texto}"


def test_ninguna_respuesta_rapida_del_coaching_esta_inventada():
    """Si un consejo nombra una plantilla que no existe, manda al operador a buscar algo que
    no va a encontrar -- y peor, contradice al propio manual, que prohibe alterar o inventar
    respuestas rapidas (error critico E10). El catalogo es src/catalogo_atc.py."""
    barra = re.compile(r"/[A-Za-z][A-Za-z0-9áéíóúñ]*")
    conocidas = {rr.lower() for rr in RESPUESTAS_RAPIDAS}
    for clave, texto in _todos_los_textos():
        for m in barra.findall(texto):
            assert m.lower() in conocidas, f"{clave} nombra {m!r}, que no está en el catálogo"


def _ngramas(texto: str, n: int = 5) -> set[str]:
    palabras = [p for p in re.findall(r"\w+", texto.lower()) if p]
    return {" ".join(palabras[i:i + n]) for i in range(len(palabras) - n + 1)}


def test_el_umbral_de_5_PALABRAS_separa_el_tema_de_la_parafrasis():
    """Por que 5 y no 4, con los dos casos reales que lo calibraron.

    Con n=4 el test marcaba tambien el consejo del 2 de `retiro`, y ese texto esta BIEN:
    comparte con su rationale la frase de dominio "que la plata salio" (4 palabras) pero
    aporta una instruccion que el reproche no tiene ("Enviar el comprobante siempre, incluso
    si el agente no lo pidio"). Compartir el TEMA es correcto y deseable.

    El caso patologico de `registro` compartia una CLAUSULA entera de 5 palabras. El umbral
    esta puesto ahi: 4 palabras es un tema, 5 seguidas es la misma oracion dos veces.
    """
    tema_compartido = ("Respondió en 30 segundos, pero nunca envió el comprobante del retiro: "
                       "el agente no tiene con qué respaldar que la plata salió.")
    instruccion = ("Enviar el comprobante siempre, incluso si el agente no lo pidió: es el "
                   "único respaldo de que la plata salió.")
    assert not (_ngramas(tema_compartido) & _ngramas(instruccion))

    # El PAR HISTORICO de `registro` 4, textual, como estaba hasta el 2026-08-13. Este
    # assert es lo que prueba que el invariante sabe atrapar la regresion que motivo el
    # cambio -- sin el, el test pasaria por no tener nada que encontrar.
    rationale_viejo = ("Creó la cuenta 1,5 minutos después de recibir los datos. No llegó a "
                       "acompañarlo hasta la primera recarga.")
    consejo_viejo = ("La cuenta quedó creada. Lo que falta es acompañarlo hasta la primera "
                     "recarga, que es donde el registro se convierte en jugador.")
    assert _ngramas(rationale_viejo) & _ngramas(consejo_viejo) == {
        "acompañarlo hasta la primera recarga"}


def test_el_consejo_no_es_la_PARAFRASIS_del_reproche():
    """El coaching no puede repetir la frase del rationale de su MISMA fila.

    EL OTRO AGUJERO, medido el 2026-08-13 sobre el rescore v13: **1.040 de 1.054 filas
    (98,7%) de `registro/determinista-v1` en 4 estrellas** tenian la MISMA frase
    ("acompañarlo hasta la primera recarga") en `rating_rationale` y en `recomendacion`.
    En el camino LLM eso pasaba 0 de 1.541 veces, asi que era puramente el texto fijo.
    Los 13 invariantes no podian verlo porque NINGUNO comparaba los dos campos de la
    misma fila: se miraba el consejo solo, en el vacio.

    Se compara con n-gramas de 4 palabras: repetir "la primera recarga" (3) es hablar del
    mismo tema, que es correcto y deseable; repetir una clausula entera es no aportar nada.
    """
    casos = [
        ("registro 4", score_registro([
            _cli(0, "quiero registrarme"), _cli(30, DATOS),
            _op(120, CREDENCIALES),
        ])),
        ("deposito 2", score_deposito([
            _cli(0, "les mando el comprobante de la recarga"), _cli(5, "", media="image"),
            _op(20, "recibido, en breve tendrás tu saldo"),
        ], BASE + timedelta(seconds=600))),
        ("retiro 2", score_retiro([
            _cli(0, "Monto a retirar: 20"), _op(30, "tu retiro está en proceso"),
        ], BASE + timedelta(seconds=600))),
    ]
    for nombre, s in casos:
        assert s is not None, nombre
        if not s.recomendacion:
            continue
        compartidos = _ngramas(s.rating_rationale) & _ngramas(s.recomendacion)
        assert not compartidos, (
            f"{nombre}: el consejo repite el reproche {sorted(compartidos)!r}\n"
            f"  rationale: {s.rating_rationale}\n"
            f"  consejo  : {s.recomendacion}")


# --- EL CONSEJO DEL 4 TIENE QUE PEDIR LAS DOS COSAS ---------------------------------
# `operator_asked_and_waited` exige DOS condiciones para dar el 5: que el operador pregunte
# "¿algo más?" Y que deje una ventana real antes de cerrar el ticket (que el cliente conteste,
# o que pasen 5 minutos). El coaching solo hablaba de la primera.
#
# MEDIDO el 2026-08-13 sobre 273 sesiones donde el gate dio False:
#   134 (49%)  no dijeron nada parecido a una pregunta de cierre
#    58 (21%)  SI preguntaron y los rechazo la ESPERA, no el vocabulario
#    81 (30%)  dijeron una DESPEDIDA ("escríbeme cuando quieras"), que empuja al futuro
#              en vez de retener la conversacion abierta -- no es el mismo acto
# Ese 21% pregunta, cierra el ticket en el mismo acto, y nunca va a entender por que no llega
# al 5 si el consejo no le dice que espere. Cumplimiento global: 8 de 122 (6,6%).

_MODULOS_CON_CIERRE = ("deposito", "retiro", "info", "soporte")


def _coaching_del_cuatro():
    """El consejo de 4 estrellas de cada rubrica que tiene pregunta de cierre.

    Busca en las DOS fuentes mientras dure la migracion al catalogo: `info` ya esta en
    src/catalogo_coaching.py y `deposito`/`retiro`/`soporte` todavia tienen su `_COACHING`
    local. Asi el invariante no pierde cobertura durante la mudanza.
    """
    import importlib

    from src.catalogo_coaching import CONSEJOS

    # SOLO EL SEGMENTO JUGADOR. Este invariante es el estandar de cierre DEL JUGADOR: "¿te
    # falta algo más?" mas la espera. El manual le da al agente otro -- "debido a que muchos
    # no responden despues de recibir la informacion, el operador PUEDE cerrar el chat cuando
    # el caso haya sido resuelto" --, asi que exigirle la pregunta seria pedirle algo que el
    # propio manual releva. La variante de agente la cubre
    # tests/test_catalogo_coaching.py::test_el_cierre_del_agente_no_le_exige_la_pregunta.
    del_catalogo = {c.rubrica: c.texto for c in CONSEJOS
                    if c.situacion == "4" and c.segmento == "jugador"}
    for nombre in _MODULOS_CON_CIERRE:
        if nombre in del_catalogo:
            yield nombre, del_catalogo[nombre]
            continue
        mod = importlib.import_module(f"src.{nombre}")
        yield nombre, mod._COACHING[4]


def test_el_consejo_del_4_pide_LA_PREGUNTA():
    for nombre, texto in _coaching_del_cuatro():
        assert "algo más" in texto.lower(), f"{nombre}: no nombra la pregunta de cierre: {texto}"


def test_el_consejo_del_4_pide_TAMBIEN_LA_ESPERA():
    # La mitad que faltaba: sin esto, el operador pregunta y cierra en el mismo acto.
    for nombre, texto in _coaching_del_cuatro():
        bajo = texto.lower()
        assert "5 minutos" in bajo, f"{nombre}: no dice cuánto esperar: {texto}"
        assert any(p in bajo for p in ("antes de cerrar", "sin cerrar", "no cerrar")), \
            f"{nombre}: no dice que la espera es ANTES de cerrar: {texto}"
