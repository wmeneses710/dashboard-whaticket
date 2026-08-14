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


# === RETIRADA EL 2026-08-14 ==========================================================
# La decision de v14 que testeaba este archivo SE REVIRTIO. `cliente_reinsistio` ya no
# alimenta `friccion` ni `confuso_corroborado`. Los tests de abajo son ahora el guard de
# esa retirada: verifican que la señal del modelo NO mueve la nota.
#
# POR QUE. Analisis del comportamiento sobre las 103 filas de v15 donde la señal dispara
# (categorias NO excluyentes):
#     39%  RAFAGA (mediana entre mensajes < 60 s: como escribe la gente)
#     14%  PLANTILLA justo antes de repetir  <- LO UNICO que se queria medir
#     11%  DUPLICADO literal del mismo mensaje
#      7%  el cliente nunca escribio 2 veces seguidas (imposible reinsistir)
# Acierta su objetivo 14 de 103 veces. El 86% restante es ruido.
#
# PARA QUE SE HABIA CREADO: detectar al operador que despacha con una PLANTILLA generica
# en vez de contestar el motivo, y el cliente tiene que volver a preguntar. Se diseño
# ANTES de que existieran los MOTIVOS, cuando no se sabia que queria el cliente.
#
# POR QUE NO SE ARREGLA CON UN PROMPT MEJOR: el fenomeno real es el **0,3%** del padron
# (7 de 2.760 filas, medido con un detector determinista de plantillas), y el piso de
# ruido del modelo en `registro` es **9,3%**. La inestabilidad del instrumento es mas de
# un orden de magnitud mayor que la cosa a medir. Es un limite de instrumentacion.
#
# POR QUE NO SE REEMPLAZA POR EL DETECTOR DETERMINISTA: necesita un diccionario de
# plantillas, y el diccionario es un blanco movil -- 212 plantillas globales contra
# **1.675 propias de un operador** repartidas en 55 operadores, y una plantilla recien
# creada tiene cero usos, o sea que es invisible justo cuando mas querrias detectarla.
#
# LO QUE OCUPA SU LUGAR: el eje de CLARIDAD. "La respuesta no contesto" es exactamente
# `claridad='confuso'` corroborado por `(asked and not resolved and not pushed)` -- el
# esquive genuino--, que no depende de esta señal y sigue en pie.

def test_la_reinsistencia_del_LLM_ya_NO_demota():
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.friccion is False
    assert r.rating_label == "buena" and r.stars == 4


def test_la_señal_del_modelo_se_sigue_persistiendo_para_poder_medirla():
    # No se borra el dato crudo: si algun dia hay un instrumento que lo detecte, hace falta
    # el historico para compararlo.
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.dimensions.get("cliente_reinsistio") is True


def test_sin_reinsistencia_la_nota_tampoco_se_mueve():
    r = score_by_motivo(target_messages=SIN_RESOLVER, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=False)))
    assert r.rating_label == "buena" and r.stars == 4


def test_la_resolucion_determinista_sigue_protegiendo_el_piso():
    # "lo determinista gana": el cliente insistio pero la operacion se completo.
    r = score_by_motivo(target_messages=RESUELTO, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=True)))
    assert r.rating_label == "buena" and r.stars == 4


def test_un_excelente_ya_NO_se_cae_por_la_reinsistencia_del_modelo():
    r = score_by_motivo(
        target_messages=SIN_RESOLVER, thread_context="",
        llm=FakeLLM(_resp(cliente_reinsistio=True, hizo_accion_extra=True,
                          cortesia_destacada=True)))
    assert r.friccion is False


def test_el_SILENCIO_MEDIDO_sigue_demotando():
    """Lo que NO se retiro: `client_reasked`, que exige silencio real con timestamps."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)

    def cli(mins, body):
        return {"created_at": base + timedelta(minutes=mins), "from_me": False,
                "is_note": False, "body": body, "media_type": "chat"}

    msgs = [cli(0, "necesito ayuda"), cli(7, "hola?"), cli(9, "alguien ahi"),
            cli(12, "me responden")]
    r = score_by_motivo(target_messages=msgs, thread_context="",
                        llm=FakeLLM(_resp(cliente_reinsistio=False)))
    assert r.friccion is True
    assert r.stars <= 2


# --- EL ABANDONO NO HABILITA EL MEJOR ESCENARIO DE `registro` -----------------------
# `src/scorer.py` desactivaba el techo ENTERO de `registro` cuando el cliente abandonaba, y
# eso incluía la parte que no habla de culpa sino de un HECHO: llegar al fall-through con
# motivo `registro` prueba que el alta NO se cerró, y el mejor escenario del motivo es —
# textual en src/rubrics.py — "cierra el alta y encamina el primer depósito".
#
# MEDIDO el 2026-08-13: **45 filas de `registro` por el camino LLM con `cliente_abandono=true`
# en 5 estrellas**, contra 0 de las 2.061 con abandono=false (ahí el techo funciona perfecto).
# Cuatro de ellas, verbatim, son una campaña de broadcast de la misma operadora con este
# rationale y `rating_label='excelente'`:
#     "El operador atendió el motivo de registro al explicar el proceso, pero NO GUIO AL
#      CLIENTE PASO A PASO NI LE PIDIÓ LOS DATOS NECESARIOS PARA CREAR LA CUENTA."
# La nota máxima de la escala con un texto que la desmiente.
#
# POR QUE NO ALCANZABA CON EXIGIR SEÑAL DURA: de esas 45, **41 tienen `pushed=True`** (mencionan
# el bono o mandan el link), así que un guard sobre `resolved or pushed` habría atrapado 1 sola.
# Lo que SI es universal: **`se_creo_la_cuenta` da False en las 45**.
#
# QUE SE CONSERVA de la corrección del 2026-08-07: esa decisión existe para no capear a
# 'aceptable' al operador "que ofreció crear la cuenta y se quedó esperando una respuesta que
# nunca llegó -- el segundo hizo lo que podía". Eso sigue igual: con abandono NO se lo baja a
# 'aceptable'. Lo único que se le saca es el 'excelente', que nunca fue suyo.

def _resp_registro(**over):
    resp = _resp(motivo="registro")
    resp["hizo_accion_extra"] = True
    resp["cortesia_destacada"] = True
    resp.update(over)
    return resp


# El cliente pide, el operador ofrece el bono y el link (pushed) y le PREGUNTA, y el cliente
# no vuelve: eso es `cliente_abandono_tras_pedido`. El ultimo mensaje del negocio tiene que
# ser un PEDIDO (`_es_pedido`) para que el desenlace sea "se_fue" -- sin eso el test pasa por
# la razon equivocada, que fue exactamente lo que paso al escribirlo.
# El que SOLO RECITA LA PLANTILLA: describe lo que tiene que hacer el cliente ("te
# registras") y le pasa su numero personal. Nunca ofrecio hacer el alta. Es el caso `9a83a433`.
ABANDONO_SIN_OFERTA = [
    _cli("¿Cómo accedo a sortiGo para recibir los beneficios.?"),
    _op("Pana te cuento es muy sencillo, trabajo para Sorti365. Te registras, verificas tu "
        "cuenta y con tu primera carga accedes a una freebet de $5 y 10 giros gratis"),
    _op("panita te paso mi número, 0991701676, ¿lo agregas?"),
]
# El que SE OFRECIO y se quedo esperando: el caso que el negocio protegio el 2026-08-07.
ABANDONO_CON_OFERTA = [
    _cli("Quiero registrarme y recibir mi Bono de $5"),
    _op("Trabajo como agente de Sorti365 y por tu primera recarga tengo una Freebet de $5"),
    _op("¿Te creo un usuario para que juegues?"),
]


def test_el_caso_de_prueba_de_verdad_tiene_abandono():
    # EL GUARD DEL GUARD: sin esto el test de abajo pasaria aunque el techo nunca se
    # desactivara, y no probaria nada.
    from src.registro import fue_al_punto
    from src.signals import cliente_abandono_tras_pedido
    for msgs in (ABANDONO_SIN_OFERTA, ABANDONO_CON_OFERTA):
        assert cliente_abandono_tras_pedido(msgs) is True
    assert fue_al_punto(ABANDONO_SIN_OFERTA) is False, "recitar la plantilla no es ofrecer"
    assert fue_al_punto(ABANDONO_CON_OFERTA) is True


def test_recitar_la_plantilla_no_alcanza_para_el_mejor_escenario():
    r = score_by_motivo(target_messages=ABANDONO_SIN_OFERTA, thread_context="",
                        llm=FakeLLM(_resp_registro()))
    assert r.motivo == "registro"
    assert r.stars < 5, r.rating_rationale


def test_el_que_SE_OFRECIO_y_el_cliente_se_fue_CONSERVA_su_nota():
    # LO QUE SE CONSERVA, textual, de la decisión del 2026-08-07: "el segundo hizo lo que
    # podía". Ofreció crear la cuenta y el cliente nunca volvió: la nota no se toca.
    r = score_by_motivo(target_messages=ABANDONO_CON_OFERTA, thread_context="",
                        llm=FakeLLM(_resp_registro()))
    assert r.rating_label == "excelente" and r.stars == 5, r.rating_rationale


# --- "TE REGISTRAS" NO ES UNA OFERTA DEL OPERADOR ----------------------------------
# `_AL_PUNTO_RE` tenia el grupo de la ayuda OPCIONAL --  `te (ayudo (a |con (el|tu|mi) )?)?registr`
# -- asi que colapsaba a `te registr` a secas y matcheaba "TE REGISTRAS", que es lo que hace el
# CLIENTE, no lo que ofrece el operador.
# CASO REAL `9a83a433` (Salome Ramirez), el mensaje entero del operador:
#     "Pana te cuento es muy sencillo, trabajo para Sorti365... TE REGISTRAS, verificas tu
#      cuenta y con tu primera carga accedes a una freebet de $5 y 10 giros gratis"
#     y despues: "panita te paso mi número, 0991701676"
# El regex lo leia como "fue al punto"; el rationale del LLM decia "no guio al cliente paso a
# paso ni le pidio los datos". **El rationale tenia razon y el regex no.**
# Se conservan las dos formas que el patron queria: "te ayudo a registrarte" / "te ayudo con el
# registro" (la segunda se agrego el 2026-08-07 por el caso `ec84aae1`) y la primera persona
# "te registro". Lo que sale es la segunda persona del cliente.

def test_te_registras_no_es_una_oferta_del_operador():
    from src.registro import _AL_PUNTO_RE
    for texto in ("Te registras, verificas tu cuenta y con tu primera carga accedes a una freebet",
                  "te registras en la web y listo",
                  "primero te registras vos"):
        assert not _AL_PUNTO_RE.search(texto), texto


def test_las_ofertas_REALES_siguen_reconociendose():
    from src.registro import _AL_PUNTO_RE
    for texto in ("¿Te creo un usuario para que juegues?",   # el caso del 2026-08-07
                  "te ayudo a registrarte",
                  "te ayudo con el registro",                 # caso ec84aae1
                  "te registro yo ahora mismo",
                  "pasame tus datos",
                  "quieres que te cree la cuenta"):
        assert _AL_PUNTO_RE.search(texto), texto
