"""Tests del armado del prompt del scorer v2 (por motivo) y del esquema de salida.

Reglas clave:
  - excluir notas internas (is_note) del transcript,
  - dar el hilo del ticket como CONTEXTO,
  - mostrar la tabla de motivos y pedir el campo `motivo` + reglas de 2 capas.
"""
from src.prompts import build_motivo_prompt, build_motivo_schema, format_transcript
from src.rubrics import MOTIVOS

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


def test_transcript_rotula_cliente_y_agente():
    t = format_transcript(MSGS_HUMAN, "human")
    assert "Cliente:" in t
    assert "Agente:" in t


def test_transcript_bot_rotula_al_bot():
    t = format_transcript([{"from_me": True, "is_note": False, "body": "soy el bot"}], "bot")
    assert "Bot:" in t


def test_transcript_motivo_rotula_negocio_como_agente():
    # Con una rubrica de motivo (no esta en _BUSINESS_LABEL) el negocio se rotula 'Agente'.
    t = format_transcript(MSGS_HUMAN, "deposito")
    assert "Agente:" in t and "Cliente:" in t


def test_transcript_trunca_conversaciones_muy_largas():
    msgs = [{"from_me": i % 2 == 0, "is_note": False, "body": f"m{i}"} for i in range(200)]
    t = format_transcript(msgs, "human")
    lineas = t.splitlines()
    assert len(lineas) < 200            # se recorto
    assert "m0" in t                    # conserva la cabeza (el motivo)
    assert "m199" in t                  # conserva la cola (el cierre)
    assert "omitidos" in t              # marca del recorte


# --- pase v2: build_motivo_prompt / build_motivo_schema -----------------------
def test_motivo_prompt_muestra_la_tabla_de_los_siete_motivos():
    system, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="")
    low = system.lower()
    for m in MOTIVOS:
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


def test_motivo_prompt_hint_de_deposito_es_condicional():
    s_no, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="", deposit_hint=False)
    s_si, _ = build_motivo_prompt(MSGS_HUMAN, thread_context="", deposit_hint=True)
    assert "HINT DETERMINISTA" in s_si
    assert "HINT DETERMINISTA" not in s_no


def test_motivo_schema_pide_motivo_dimensiones_y_hechos():
    sch = build_motivo_schema()
    props = sch["properties"]
    assert props["motivo"]["enum"] == list(MOTIVOS)
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
