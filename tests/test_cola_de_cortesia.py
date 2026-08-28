"""CAPA 2: el modelo decide si un fragmento sin respuesta del negocio es una cola de cortesia.

POR QUE HACE FALTA UNA SEGUNDA CAPA. `_solo_cortesia_del_cliente` ya usa
`signals.client_sin_motivo`, que es determinista, gratis y cubre la mayoria. Pero un patron
no absorbe todo: MEDIDO sobre 30 dias, de los 85 fragmentos que hoy cobran 1 estrella por
"nadie le respondio", **61 los salva la capa determinista y quedan 24** -- menos de UNA
inferencia por dia. Ese residuo es donde vive el caso que el negocio trajo: `'Mut amable'`,
que ni `_CORTESIA_RE` ni `client_sin_motivo` reconocen.

LA POLARIDAD DEL RIESGO MANDA, y el negocio la dicto: "no es lo mismo no responder a un
gracias, a un ok o a un listo, que no responder a una pregunta". Dejar pasar un reclamo
esconde una falla real; castigar un agradecimiento cuesta una estrella. Por eso:

  * el prompt dice explicitamente que ante la duda se PUNTUA;
  * CUALQUIER fallo -- sin LLM, timeout, JSON roto, respuesta fuera del enum -- devuelve
    None, y `None` significa "no se pudo decidir" => el llamador puntua, que es el
    comportamiento de hoy. **Nunca se pierde un reclamo por una inferencia que no llego.**

MEDIDO CONTRA gemma4:12b sobre 77 casos reales, muestra MIXTA y balanceada (33 IGNORAR /
44 PUNTUAR, asi que "ignorar a todo" saca 42,9% y no 100%):

    acierto global ................. 76/77 (98,7%)
    planteos que dejaria pasar ......  0 de 44   <- el error CARO
    gracias que castigaria ..........  1 de 33   <- el barato, y fue un '💸' suelto
                                                    que la capa 1 ya resuelve antes

EL MODELO NO ENTRA EN EL CORTE, y eso no es negociable: `partir_en_interacciones` define la
PK (`interaccion_id` = uuid5 de session_id + instante), asi que un corte no determinista da
ids distintos en cada rescore y filas huerfanas; y `queries.py` lo llama SINCRONICO por
request del tablero. Esto corre DESPUES del corte, sobre un fragmento ya cerrado, y es una
decision de NOTA.
"""
import pytest

from src.cola_de_cortesia import (
    COMPONENTE,
    decidir_con_el_modelo,
    necesita_el_modelo,
)


def _cli(texto):
    return {"from_me": False, "is_note": False, "body": texto, "media_type": "chat",
            "created_at": None}


def _op(texto):
    return {"from_me": True, "is_note": False, "body": texto, "media_type": "chat",
            "created_at": None}


class _LLM:
    """LLM de mentira. `respuesta` puede ser un dict o una excepcion a levantar."""

    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.llamadas = []

    def chat_json(self, system, user, schema=None):
        self.llamadas.append((system, user, schema))
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


# --- el gate: a quien SE LE PREGUNTA ----------------------------------------------------

def test_no_se_le_pregunta_al_modelo_si_la_capa_1_ya_lo_resolvio():
    """0,8 inferencias por dia sale de esto: la capa determinista atiende 61 de 85."""
    assert necesita_el_modelo([_cli("Muchas gracias")]) is False


def test_no_se_le_pregunta_si_el_negocio_SI_escribio():
    """Si hubo respuesta del negocio no hay nada que ignorar: la atencion existio."""
    assert necesita_el_modelo([_cli("Mut amable"), _op("un placer")]) is False


def test_SI_se_le_pregunta_por_el_residuo():
    """El caso real de Mario: ni `_CORTESIA_RE` ni `client_sin_motivo` reconocen 'Mut amable'."""
    assert necesita_el_modelo([_cli("Mut amable")]) is True


def test_no_se_le_pregunta_por_un_fragmento_con_MEDIA():
    """Un comprobante despues del cierre es un planteo, no una cola. Ya lo decidia la capa 1
    y no se afloja aca."""
    media = {"from_me": False, "is_note": False, "body": "", "media_type": "image",
             "created_at": None}
    assert necesita_el_modelo([media]) is False


def test_no_se_le_pregunta_por_un_fragmento_sin_mensajes_reales():
    nota = {"from_me": True, "is_note": True, "body": "Ana *resuelto*", "media_type": "chat",
            "created_at": None}
    assert necesita_el_modelo([nota]) is False


# --- la decision ------------------------------------------------------------------------

def test_IGNORAR_devuelve_True():
    llm = _LLM({"decision": "IGNORAR", "cita": "Mut amable"})
    assert decidir_con_el_modelo([_cli("Mut amable")], "tu saldo ya esta disponible", llm) is True


def test_PUNTUAR_devuelve_False():
    llm = _LLM({"decision": "PUNTUAR", "cita": "no me acreditaron"})
    assert decidir_con_el_modelo([_cli("Ok no me acreditaron")], "", llm) is False


def test_el_prompt_lleva_lo_ULTIMO_que_dijo_el_negocio():
    """Sin eso el modelo no puede saber si el cliente esta confirmando algo o planteando
    algo nuevo: 'ya esta' contesta a 'tu saldo ya esta disponible' y plantea, solo."""
    llm = _LLM({"decision": "IGNORAR", "cita": "x"})
    decidir_con_el_modelo([_cli("ya esta")], "tu saldo ya esta disponible", llm)
    _, user, _ = llm.llamadas[0]
    assert "tu saldo ya esta disponible" in user
    assert "ya esta" in user


def test_el_prompt_declara_la_POLARIDAD_del_riesgo():
    """El negocio lo dicto: no es lo mismo no responder un gracias que no responder una
    pregunta. Si esa instruccion se cae, el modelo pierde el sesgo que lo hace seguro."""
    llm = _LLM({"decision": "IGNORAR", "cita": "x"})
    decidir_con_el_modelo([_cli("gracias")], "", llm)
    system, _, _ = llm.llamadas[0]
    assert "PUNTUAR" in system and "duda" in system.lower()


def test_la_opcion_de_IGNORAR_existe_en_el_enum():
    """Trampa 1 de scripts/bench_sin_motivo.py: si no se le ofrece la opcion, el modelo
    elige entre las que hay y eso mide el prompt, no al modelo."""
    llm = _LLM({"decision": "IGNORAR", "cita": "x"})
    decidir_con_el_modelo([_cli("gracias")], "", llm)
    _, _, schema = llm.llamadas[0]
    assert set(schema["properties"]["decision"]["enum"]) == {"IGNORAR", "PUNTUAR"}


# --- LA REGLA DE ORO: ante cualquier duda, se PUNTUA -------------------------------------

def test_sin_llm_no_decide_y_por_lo_tanto_se_PUNTUA():
    """El worker corre en local y en tests sin modelo. Ausencia de LLM no puede volverse
    'ignoralo'."""
    assert decidir_con_el_modelo([_cli("Mut amable")], "", None) is None


def test_si_el_modelo_REVIENTA_no_decide():
    llm = _LLM(RuntimeError("timeout"))
    assert decidir_con_el_modelo([_cli("Mut amable")], "", llm) is None


def test_si_el_modelo_contesta_CUALQUIER_COSA_no_decide():
    """Fuera del enum es lo mismo que no haber contestado. Un 'quiza' no puede leerse como
    IGNORAR."""
    for basura in ({"decision": "QUIZA"}, {"decision": None}, {}, {"otra": "cosa"}):
        assert decidir_con_el_modelo([_cli("Mut amable")], "", _LLM(basura)) is None


def test_None_NO_es_False_y_el_llamador_tiene_que_poder_distinguirlos():
    """`False` = el modelo dijo PUNTUAR. `None` = no se pudo decidir. Los dos terminan
    puntuando hoy, pero solo el segundo es un fallo que hay que poder contar."""
    assert decidir_con_el_modelo([_cli("x")], "", None) is None
    assert decidir_con_el_modelo([_cli("x")], "", _LLM({"decision": "PUNTUAR"})) is False


def test_el_componente_para_la_bitacora_esta_declarado():
    """Los fallos van a la tabla `errors` compartida (ver src/errores.py), y su vocabulario
    de componentes esta acordado."""
    from src.errores import COMPONENTES

    assert COMPONENTE in COMPONENTES


# --- EL CABLEADO EN EL WORKER -----------------------------------------------------------
#
# DONDE VA Y POR QUE AHI. `score_sin_respuesta` corre PRIMERO en `_score_interaccion_y_persiste`
# y esa prioridad es del negocio ("si no hubo respuesta, ese caso manda"). No se invierte: se
# le pone una compuerta ADELANTE que solo se abre para el residuo, y solo si el modelo dice
# que no habia nada que contestar. Todo lo demas sigue igual.

from src import worker


def _sess():
    """Misma forma que devuelve `fetch_pending_sessions` (ver tests/test_worker._session_row):
    la conversacion de ENTRADA mas el session_id, con `id == session_id`."""
    return {"id": "s1", "account": "datos", "ticket_id": "t1", "user_id": None,
            "created_at": None, "first_sent_message_at": None, "resolved_at": None,
            "queue_name": None, "channel": None, "session_id": "s1",
            "is_group": False, "contact_number": "0999", "linea_propia": None}


def _frag(texto, media=None):
    from datetime import datetime, timezone
    t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    return [{"created_at": t, "from_me": False, "is_note": False, "body": texto,
             "media_type": media or "chat", "sent_from": None, "user_id": None}]


def test_el_modelo_dice_IGNORAR_y_el_fragmento_NO_cobra_1_estrella(monkeypatch):
    """El caso de Mario: la atencion la dio Ramirez y el 'gracias' cayo en otro operador."""
    monkeypatch.setattr(worker, "decidir_con_el_modelo", lambda *a, **k: True)
    ev, sr, score = worker._disposicion_de_la_cola(
        _frag("Mut amable"), ultimo_del_negocio="tu saldo ya esta disponible", llm=object())
    assert (ev, sr) == ("skipped", "cola_de_cortesia")
    assert score is None


def test_el_modelo_dice_PUNTUAR_y_el_1_estrella_QUEDA(monkeypatch):
    """El error caro. Si el modelo ve un planteo, la nota es la de siempre."""
    monkeypatch.setattr(worker, "decidir_con_el_modelo", lambda *a, **k: False)
    assert worker._disposicion_de_la_cola(
        _frag("Ok no me acreditaron"), ultimo_del_negocio="", llm=object()) is None


def test_si_el_modelo_NO_PUDO_decidir_el_1_estrella_QUEDA(monkeypatch):
    """LA REGLA DE ORO: una inferencia que no llego no puede borrar una falla."""
    monkeypatch.setattr(worker, "decidir_con_el_modelo", lambda *a, **k: None)
    assert worker._disposicion_de_la_cola(
        _frag("Mut amable"), ultimo_del_negocio="", llm=object()) is None


def test_sin_llm_ni_se_pregunta_y_el_1_estrella_QUEDA():
    """El worker corre en local y en tests sin modelo: el comportamiento por defecto es el
    de hoy, no 'ignoralo'."""
    assert worker._disposicion_de_la_cola(
        _frag("Mut amable"), ultimo_del_negocio="", llm=None) is None


def test_al_fragmento_que_la_capa_1_ya_resuelve_NI_SE_LE_PREGUNTA(monkeypatch):
    """Los 61 de 85 que no cuestan inferencia. Si esto se rompe, el costo se dispara."""
    preguntas = []
    monkeypatch.setattr(worker, "decidir_con_el_modelo",
                        lambda *a, **k: preguntas.append(1) or True)
    worker._disposicion_de_la_cola(_frag("Muchas gracias"), ultimo_del_negocio="",
                                   llm=object())
    assert preguntas == [], "le pregunto al modelo por algo que la capa 1 ya resolvia"


def test_al_fragmento_con_MEDIA_ni_se_le_pregunta(monkeypatch):
    """Un comprobante despues del cierre es un planteo. Es la segunda barrera de la
    polaridad del riesgo: lo que no parece cola ni se pregunta."""
    preguntas = []
    monkeypatch.setattr(worker, "decidir_con_el_modelo",
                        lambda *a, **k: preguntas.append(1) or True)
    worker._disposicion_de_la_cola(_frag("", media="image"), ultimo_del_negocio="",
                                   llm=object())
    assert preguntas == []


def test_un_fallo_del_modelo_NO_tumba_el_scoring(monkeypatch):
    """`decidir_con_el_modelo` promete no levantar, pero el worker no puede depender de esa
    promesa -- misma cautela que `_registrar_fallo`."""
    def revienta(*a, **k):
        raise RuntimeError("el modelo")
    monkeypatch.setattr(worker, "decidir_con_el_modelo", revienta)
    assert worker._disposicion_de_la_cola(
        _frag("Mut amable"), ultimo_del_negocio="", llm=object()) is None


def test_la_causa_del_skip_esta_en_el_front():
    """Si la causa no esta en SKIP_LABEL, la tarjeta de 'sin evaluar' muestra el codigo
    crudo y el negocio pierde de vista a donde se fueron esas filas -- que es justo el
    problema que saco a `redireccion` de ser skip el 2026-08-20."""
    from pathlib import Path
    front = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "cola_de_cortesia:" in front, "falta la etiqueta en SKIP_LABEL"


# --- END TO END: que la fila SE PERSISTA, o la sesion vuelve como pendiente para siempre --
#
# EL RIESGO REAL DE ESTE CAMBIO no es la nota: es que un fragmento que deja de tener score
# no persista fila. `fetch_pending_sessions` trae las sesiones que "aun NO fueron scoreadas",
# asi que una sesion sin fila vuelve en CADA pasada del worker -- un bucle silencioso que se
# ve como "el worker esta lento". Es el mismo modo de falla que `src/llm.py:207` documenta,
# donde una sesion volvia a la cabeza de la cola y fallo ~15 veces en tres horas.

class _LLMDice:
    """Solo contesta la compuerta. El pase v2 por motivo se stubea aparte.

    En produccion el worker le pasa el MISMO objeto `llm` a las dos cosas, y este falso
    empezo intentando servir a ambas -- pero reproducir el contrato completo de v2
    (dimensiones anidadas incluidas) es pelearse con otro modulo dentro de un test que mide
    la compuerta. Se stubea `score_by_motivo` y el alcance queda honesto.
    """

    def __init__(self, decision):
        self.decision = decision
        self.decisiones = []

    def chat_json(self, system, user, schema=None):
        props = ((schema or {}).get("properties") or {})
        assert "decision" in props, "el pase v2 tendria que estar stubeado en este test"
        self.decisiones.append(user)
        return {"decision": self.decision, "cita": "x"}


def _sesion_con_cola():
    """La forma del caso de Mario: atencion buena, cierre, y un 'gracias' con typo."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    def m(off, fm, body, note=False):
        return {"created_at": t0 + timedelta(seconds=off), "from_me": fm, "is_note": note,
                "body": body, "media_type": "chat", "sent_from": "WEB" if fm else None,
                "user_id": "u1" if fm else None, "ack": 3}
    return [
        m(0, False, "comprobante"),
        m(120, True, "*Ramirez:* ¡Gracias por tu recarga! Tu saldo ya está disponible"),
        m(121, True, "Ramirez *resuelto* la conversación", note=True),
        m(133, False, "Mut amable"),
        m(134, True, "*Asignado automáticamente* a Mario", note=True),
        m(208, True, "Mario *resuelto* la conversación", note=True),
    ]


def _corre_e2e(monkeypatch, llm):
    """Corre `score_session_and_store` de punta a punta contra fakes, y devuelve las filas
    que se PERSISTIERON."""
    from src.scorer import ScoreResult
    persistidas = []
    # El pase v2 no es lo que este test mide: se stubea con una nota cualquiera para que la
    # atencion buena de la sesion tenga fila y se pueda comprobar que la compuerta NO se la
    # comio.
    monkeypatch.setattr(worker, "score_by_motivo", lambda **k: ScoreResult(
        rubric="human", dimensions={}, rating_label="buena", rating_rationale="ok",
        stars=4, llm_model="fake", atencion=None, deposit_observed=None,
        motivo="deposito"))
    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: _sesion_con_cola())
    monkeypatch.setattr(worker, "upsert_score", lambda cur, rec: persistidas.append(rec))
    monkeypatch.setattr(worker, "build_operator_map", lambda cur: {})

    class _Cur:
        description = []
        def execute(self, *a, **k): pass
        def fetchall(self): return []
        def fetchone(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def rollback(self): pass

    resultado = worker.score_session_and_store(_Conn(), _sess(), llm, {}, None, {})
    return persistidas, resultado


def test_e2e_la_fila_del_skip_SE_PERSISTE_igual(monkeypatch):
    """Si no persiste, la sesion vuelve como pendiente en cada pasada: bucle silencioso."""
    persistidas, _ = _corre_e2e(monkeypatch, _LLMDice("IGNORAR"))
    assert persistidas, "no persistio NINGUNA fila: la sesion volveria como pendiente"
    colas = [r for r in persistidas if r.get("skip_reason") == "cola_de_cortesia"]
    assert len(colas) == 1, f"esperaba una fila de cola; hubo {len(colas)} de {len(persistidas)}"
    assert colas[0]["eval_status"] == "skipped"
    assert colas[0].get("stars") is None


def test_e2e_las_OTRAS_interacciones_de_la_sesion_no_se_tocan(monkeypatch):
    """La compuerta afecta al fragmento, no a la sesion: la atencion de Ramirez sigue con su
    fila y su nota."""
    persistidas, _ = _corre_e2e(monkeypatch, _LLMDice("IGNORAR"))
    assert len(persistidas) >= 2, "se perdio la atencion buena al saltear la cola"
    con_nota = [r for r in persistidas if r.get("stars") is not None]
    assert con_nota, "ninguna interaccion quedo con nota: la compuerta se comio la sesion"


def test_e2e_si_el_modelo_dice_PUNTUAR_la_fila_conserva_su_1_estrella(monkeypatch):
    persistidas, _ = _corre_e2e(monkeypatch, _LLMDice("PUNTUAR"))
    assert not [r for r in persistidas if r.get("skip_reason") == "cola_de_cortesia"]
    unas = [r for r in persistidas if r.get("stars") == 1]
    assert unas, "el 1 estrella desaparecio sin que el modelo lo pidiera"


def test_e2e_SIN_llm_el_comportamiento_es_EXACTAMENTE_el_de_hoy(monkeypatch):
    """La red de seguridad: el worker sin modelo tiene que dar lo mismo que antes del cambio."""
    persistidas, _ = _corre_e2e(monkeypatch, None)
    assert not [r for r in persistidas if r.get("skip_reason") == "cola_de_cortesia"]
    assert [r for r in persistidas if r.get("stars") == 1]


def test_e2e_no_se_consulta_al_modelo_MAS_DE_UNA_VEZ_por_fragmento(monkeypatch):
    """Una inferencia por fragmento del residuo. Si esto se dispara, 0,8 por dia deja de ser
    cierto y el costo se vuelve otro."""
    llm = _LLMDice("IGNORAR")
    _corre_e2e(monkeypatch, llm)
    assert len(llm.decisiones) == 1, (
        f"consulto {len(llm.decisiones)} veces a la compuerta en una sesion")


# --- LA DISTANCIA NO DECIDE EL CASTIGO (decision del negocio, 2026-08-28) ---------------
#
# Textual: "Merece el skip porque para el usuario es la misma conversacion, no quiere nada
# mas, no se debe castigar a ATC por algo que no requiere atencion."
#
# Eso separa DOS cosas que estaban pegadas y no son la misma:
#   * `GRACIA_CORTESIA_SEG` (10 min) gobierna la ATRIBUCION -- si la cola se pega, el
#     operador anterior se lleva la evidencia de que el cliente quedo conforme.
#   * el CASTIGO no depende de la distancia: si el mensaje no pedia atencion, no hay falla.
#
# EL HUECO QUE ESTO CIERRA, medido sobre 30 dias: de los 65 fragmentos que cobraban 1
# estrella, **35 eran cortesia pura FUERA de la ventana** -- el bucket mas grande, y ni la
# capa 1 los pegaba ni la compuerta les preguntaba. `necesita_el_modelo` los excluia con un
# comentario que decia "ya se pegaron", y era falso.
#
# Y NO CUESTA INFERENCIA: ahi el determinista ya sabe la respuesta (`client_sin_motivo`,
# verificada 40/40 contra el modelo). El modelo sigue reservado para el residuo.

def test_la_cortesia_pura_va_a_SKIP_aunque_pase_HORAS_del_cierre():
    """El caso de Christian: 'Ok' a los 83 minutos. Nadie tiene que contestar eso."""
    ev, sr, score = worker._disposicion_de_la_cola(
        _frag("Ok"), ultimo_del_negocio="tu saldo ya esta disponible", llm=None)
    assert (ev, sr, score) == ("skipped", "cola_de_cortesia", None)


def test_la_cortesia_pura_NO_gasta_inferencia(monkeypatch):
    """El determinista ya sabe la respuesta: preguntarle al modelo seria pagar por nada."""
    preguntas = []
    monkeypatch.setattr(worker, "decidir_con_el_modelo",
                        lambda *a, **k: preguntas.append(1) or True)
    worker._disposicion_de_la_cola(_frag("Muchas gracias"), ultimo_del_negocio="",
                                   llm=object())
    assert preguntas == [], "gasto una inferencia en algo que la capa 1 ya resolvia"


def test_la_cortesia_pura_va_a_skip_INCLUSO_SIN_MODELO():
    """Es determinista: no depende de que el LLM este arriba."""
    for texto in ("Gracias", "Ok", "Listo", "🫡", "Tks", "Ya", "Muy amable", "Graciad"):
        ev, sr, _ = worker._disposicion_de_la_cola(_frag(texto), ultimo_del_negocio="",
                                                    llm=None)
        assert (ev, sr) == ("skipped", "cola_de_cortesia"), f"{texto!r} no fue a skip"


def test_lo_que_PIDE_ATENCION_sigue_cobrando_el_1_estrella():
    """El limite de la decision. 'no se debe castigar por algo que no requiere atencion'
    dice, exactamente, que lo que SI requiere atencion se sigue castigando."""
    for texto in ("Mi bono", "Ok no me acreditaron", "Bueno y cuando me acreditan?",
                  "30$ Dany Alexander Cedula 1313450387 Cuenta 2202263108 Pichincha"):
        assert worker._disposicion_de_la_cola(
            _frag(texto), ultimo_del_negocio="", llm=None) is None, (
            f"{texto!r} dejo de cobrar el 1 estrella sin que nadie lo decidiera")


def test_un_mensaje_del_negocio_en_el_fragmento_cancela_todo():
    """Si el negocio escribio, la atencion existio: no hay nada que ignorar ni que saltear."""
    frag = _frag("Gracias") + [{"created_at": None, "from_me": True, "is_note": False,
                                "body": "un placer", "media_type": "chat"}]
    assert worker._disposicion_de_la_cola(frag, ultimo_del_negocio="", llm=None) is None
