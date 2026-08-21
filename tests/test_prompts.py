"""Tests del armado del prompt del scorer v2 (por motivo) y del esquema de salida.

Reglas clave:
  - excluir notas internas (is_note) del transcript,
  - dar el hilo del ticket como CONTEXTO,
  - mostrar la tabla de motivos y pedir el campo `motivo` + reglas de 2 capas.
"""
from src.prompts import build_motivo_prompt, build_motivo_schema, format_transcript
from src.rubrics import MOTIVOS, MOTIVOS_DEL_LLM

MSGS_HUMAN = [
    {"from_me": False, "is_note": False, "body": "hola, no me llego la recarga"},
    {"from_me": True, "is_note": True, "body": "NOTA INTERNA: revisar caja"},
    {"from_me": True, "is_note": False, "body": "hola! ya te la acredito, dame un minuto"},
]


# --- format_transcript --------------------------------------------------------
def test_transcript_excluye_notas_internas():
    t = format_transcript(MSGS_HUMAN, "human")
    assert "NOTA INTERNA" not in t
    assert "no me llego la recarga" in t


def test_transcript_rotula_cliente_y_operador():
    t = format_transcript(MSGS_HUMAN, "human")
    assert "Cliente:" in t
    # OPERADOR, no "Agente": el agente es el CLIENTE vendedor (segmento `agente`).
    assert "Operador:" in t
    assert "Agente:" not in t


def test_transcript_bot_rotula_al_bot():
    t = format_transcript([{"from_me": True, "is_note": False, "body": "soy el bot"}], "bot")
    assert "Bot:" in t


def test_transcript_motivo_rotula_negocio_como_operador():
    # Con una rubrica de motivo (no esta en _BUSINESS_LABEL) el negocio se rotula 'Operador'.
    t = format_transcript(MSGS_HUMAN, "deposito")
    assert "Operador:" in t and "Cliente:" in t


def test_transcript_trunca_conversaciones_muy_largas():
    msgs = [{"from_me": i % 2 == 0, "is_note": False, "body": f"m{i}"} for i in range(200)]
    t = format_transcript(msgs, "human")
    lineas = t.splitlines()
    assert len(lineas) < 200            # se recorto
    assert "m0" in t                    # conserva la cabeza (el motivo)
    assert "m199" in t                  # conserva la cola (el cierre)
    assert "omitidos" in t              # marca del recorte


# --- pase v2: build_motivo_prompt / build_motivo_schema -----------------------
def test_motivo_prompt_muestra_la_tabla_de_los_motivos_que_el_modelo_elige():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    low = system.lower()
    for m in MOTIVOS_DEL_LLM:
        assert m in low


def test_motivo_prompt_pide_el_campo_motivo_y_reglas_de_dos_capas():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    assert '"motivo"' in system
    low = system.lower()
    assert "piso" in low and "uplift" in low


def test_motivo_prompt_incluye_transcript_y_contexto():
    _, user = build_motivo_prompt(MSGS_HUMAN, thread_context="visita previa X")
    assert "no me llego la recarga" in user
    assert "visita previa X" in user


def test_motivo_prompt_pide_atencion_y_deposit_observed():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    assert '"atencion"' in system and '"deposit_observed"' in system
    assert "empujo|pasivo|no_respondio" in system


def test_motivo_prompt_porta_reglas_generales():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    low = system.lower()
    assert "no inventes" in low
    assert "implicita" in low            # respuesta implicita
    assert "determinista" in low         # deposit_observed es observacion


def test_motivo_prompt_porta_reglas_de_negocio_de_recomendacion():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    low = system.lower()
    # regla general: no recomendar lo ya hecho (evita las contradiccions medidas)
    assert "ya hizo" in low
    # honestidad del bono / rollover
    assert "se libera" in low
    # regla transversal de la app inexistente
    assert "no hay app" in low
    # seguridad: cambio de contrasena cuando el operador crea la cuenta
    assert "cambiar la contrasena" in low or "cambie la contrasena" in low
    # tono informal permitido (no marcarlo como error)
    assert "permitido" in low


def test_motivo_prompt_hint_de_deposito_es_condicional():
    s_no, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="", deposit_hint=False)
    s_si, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="", deposit_hint=True)
    assert "HINT DETERMINISTA" in s_si
    assert "HINT DETERMINISTA" not in s_no


def test_motivo_schema_pide_motivo_dimensiones_y_hechos():
    sch = build_motivo_schema()
    props = sch["properties"]
    # MOTIVOS_DEL_LLM: `redireccion` la decidimos con `connections` y el modelo no puede
    # verificarla, asi que no entra en el enum. Ver src/rubrics.py.
    assert props["motivo"]["enum"] == list(MOTIVOS_DEL_LLM)
    assert "redireccion" not in props["motivo"]["enum"]
    dims = props["dimensions"]["properties"]
    assert {"resolucion", "iniciativa", "cortesia", "errores"} <= set(dims)
    # el LLM emite HECHOS booleanos, NO la etiqueta (la deriva el codigo)
    hechos = {"atendio_el_motivo", "hizo_accion_extra", "cortesia_destacada", "hubo_maltrato_grave"}
    assert hechos <= set(props)
    assert all(props[h]["type"] == "boolean" for h in hechos)
    assert "rating_label" not in props
    assert hechos <= set(sch["required"])
    assert {"motivo", "dimensions", "rating_rationale"} <= set(sch["required"])
    assert "atencion" not in sch["required"]
    assert "stars" not in props


def test_motivo_schema_incluye_claridad_y_reinsistio_no_requeridos():
    sch = build_motivo_schema()
    props = sch["properties"]
    assert props["claridad"]["enum"] == ["claro", "confuso", "dudoso"]
    assert props["cliente_reinsistio"]["type"] == "boolean"
    # best-effort (como atencion): no deben ser requeridos ni tumbar un rating válido
    assert "claridad" not in sch["required"]
    assert "cliente_reinsistio" not in sch["required"]


def test_motivo_prompt_pide_claridad_y_reinsistencia():
    system, _ = build_motivo_prompt([{"from_me": False, "is_note": False, "body": "hola"}], "")
    low = system.lower()
    assert "claridad" in low and "confuso" in low and "dudoso" in low
    assert "cliente_reinsistio" in low


# MEDIDO contra qwen3:14b el 2026-08-12, con 4 formas de reinsistir de menos a mas explicita:
#   "?" suelto                                        -> reinsistio=True   (la unica que veia)
#   "me ayudan?" + "hola?"                            -> False
#   el pedido repetido TEXTUAL 3 veces                -> False
#   "llevo 40 minutos esperando" + "me estan ignorando?" -> False
# El caso mas explicito posible daba False. Causa: el texto decia "true SOLO si ... (o mando
# '?', 'ayuda')" y el modelo tomo el parentesis como LISTA CERRADA, quedandose con el signo
# de pregunta literal. Y `cliente_reinsistio` es el hecho que DEMOTA a deficiente/mala, asi
# que roto empuja las notas HACIA ARRIBA: un ghosteo con 3 mensajes del cliente salio `buena`.
# El arreglo invierte el encuadre: la regla general primero y varias formas enumeradas, con
# el "SOLO" del lado del `false`, que es el caso angosto de verdad.

def test_el_prompt_enumera_las_FORMAS_de_reinsistir_no_solo_el_signo():
    system, _ = build_motivo_prompt([{"from_me": False, "is_note": False, "body": "hola"}], "")
    low = system.lower()
    # Reclamar el silencio o la demora tiene que estar nombrado explicitamente.
    assert "reclam" in low and ("silencio" in low or "demora" in low)
    # Repetir el pedido, y que alcance UNA sola vez.
    assert "repetir" in low or "repite" in low
    assert "una sola vez" in low or "alcanza una" in low
    # El "SOLO" ya no puede estar del lado del true (era lo que lo volvia conservador).
    assert "cliente_reinsistio: true solo si" not in low


def test_el_prompt_deja_el_false_como_el_caso_angosto():
    system, _ = build_motivo_prompt([{"from_me": False, "is_note": False, "body": "hola"}], "")
    low = system.lower()
    # El false es el que lleva la restriccion: no volvio a escribir, o volvio conforme.
    i = low.find("cliente_reinsistio")
    tramo = low[i:i + 900]
    assert "false solo si" in tramo, tramo[:200]


# MEDIDO contra qwen3:14b el 2026-08-12 con `--repeticiones 3`: el mismo ghosteo (el cliente
# manda el comprobante, reclama el silencio, y el operador SOLO manda la despedida) alternaba
# entre `buena` y `deficiente` entre corridas. `atendio_el_motivo` es el hecho que define el
# PISO de toda la escala, asi que dado vuelta la nota se va 2 bandas.
# Causa probable: los guards anti-falso-negativo del prompt ("MEDIA ILEGIBLE: NO asumas
# fracaso", "ABANDONO DEL CLIENTE: la falta de cierre es del CLIENTE", "PLANTILLA NO es
# cortante") empujan al modelo a leer una despedida como atencion.
# El corte NO puede ser "plantilla si / plantilla no": "listo"/"ing"/"cargado" ACUSAN el
# pedido y tienen que seguir contando. Lo que no cuenta es la despedida, que no dice NADA
# del pedido.

def test_el_prompt_dice_que_una_despedida_NO_atiende():
    system, _ = build_motivo_prompt([{"from_me": False, "is_note": False, "body": "hola"}], "")
    low = system.lower()
    # El BULLET de la definicion del hecho, no la primera mencion (que es la regla de
    # "cliente sin necesidad", mas arriba).
    i = low.find("- atendio_el_motivo:")
    assert i > 0, "no se encontro la definicion del hecho"
    tramo = low[i:i + 1400]
    assert "despedida" in tramo, tramo[:300]
    assert "mucha suerte" in tramo, tramo[:300]
    # Y el contraste tiene que estar: la plantilla que ACUSA sigue contando.
    assert "acusan" in tramo or "acusa" in tramo, tramo[:300]
    assert "listo" in tramo


# --- EXPERIMENTO: tiempos y fronteras en el transcript -----------------------------
# El modelo NO VE TIEMPOS: `format_transcript` emite `f"{who}: {body}"`. Se le pide juzgar
# calidad de atencion y no puede saber si contestaron en 20 segundos o en 20 horas. Y filtra
# `is_note`, asi que una sesion de 17 interacciones le llega como un chat PLANO.
# `con_tiempos=True` los agrega, y va APAGADO por defecto: darle tiempos crudos tiene un
# riesgo real y medible -- el proyecto ya resolvio el confound del HORARIO de forma
# determinista (`espera_efectiva` descuenta la madrugada; el 26% de los deficientes eran
# clientes que escribian de noche). Un modelo con timestamps y sin ese contexto castigaria
# una espera nocturna legitima, o la demora propia de un proceso que necesita validacion.
# Se prende recien si el banco de casos (scripts/eval_prompt.py) demuestra que gana precision
# SIN romper los casos de espera legitima.

def _m_t(seg, from_me, body, note=False):
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
    return {"created_at": base + timedelta(seconds=seg), "from_me": from_me,
            "is_note": note, "body": body, "media_type": "chat"}


def test_sin_tiempos_el_transcript_no_cambia():
    msgs = [_m_t(0, False, "hola"), _m_t(600, True, "buenas")]
    assert format_transcript(msgs, "human") == "Cliente: hola\nOperador: buenas"


def test_con_tiempos_marca_la_hora_y_el_delta():
    msgs = [_m_t(0, False, "hola"), _m_t(600, True, "buenas")]
    t = format_transcript(msgs, "human", con_tiempos=True)
    # Hora del reloj en el primer mensaje (para que pueda razonar el HORARIO) y delta despues.
    assert "[03/08 10:00]" in t
    assert "+10 min" in t


def test_con_tiempos_marca_la_frontera_de_la_interaccion():
    msgs = [_m_t(0, False, "hola"), _m_t(60, True, "listo"),
            _m_t(90, True, "Mario *resuelto* la conversación", note=True),
            _m_t(90000, False, "otra cosa")]
    t = format_transcript(msgs, "human", con_tiempos=True)
    assert "--- el operador CERRO" in t
    # Y la nota NO se emite como un mensaje del operador.
    assert "Operador: Mario *resuelto*" not in t


def test_con_tiempos_las_notas_que_no_son_cierre_siguen_afuera():
    msgs = [_m_t(0, False, "hola"),
            _m_t(1, True, "*Asignado automáticamente* a Mario", note=True),
            _m_t(60, True, "listo")]
    t = format_transcript(msgs, "human", con_tiempos=True)
    assert "Asignado" not in t
