"""Tests del armado del prompt del scorer y del esquema de salida.

Reglas clave que el prompt debe respetar:
  - excluir notas internas (is_note) del transcript que ve el LLM,
  - dar el hilo del ticket como CONTEXTO (no calificarlo),
  - inyectar los criterios y las etiquetas de la rubrica correspondiente.
"""
from src.prompts import build_output_schema, build_scorer_prompt, format_transcript

MSGS_HUMAN = [
    {"from_me": False, "is_note": False, "body": "hola, no me llego la recarga"},
    {"from_me": True, "is_note": True, "body": "NOTA INTERNA: revisar caja"},
    {"from_me": True, "is_note": False, "body": "hola! ya te la acredito, dame un minuto"},
]


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


def test_prompt_inyecta_rubrica_dominante_y_etiquetas():
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    assert "resolucion" in system            # dimension dominante de human
    assert "excelente" in system and "mala" in system  # etiquetas de human


def test_prompt_incluye_el_transcript_objetivo():
    _, user = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    assert "no me llego la recarga" in user


def test_prompt_incluye_el_contexto_del_hilo():
    _, user = build_scorer_prompt(
        "human", MSGS_HUMAN, thread_context="visita previa: el bot no resolvio"
    )
    assert "visita previa: el bot no resolvio" in user


def test_transcript_trunca_conversaciones_muy_largas():
    msgs = [{"from_me": i % 2 == 0, "is_note": False, "body": f"m{i}"} for i in range(200)]
    t = format_transcript(msgs, "human")
    lineas = t.splitlines()
    assert len(lineas) < 200            # se recorto
    assert "m0" in t                    # conserva la cabeza (el motivo)
    assert "m199" in t                  # conserva la cola (el cierre)
    assert "omitidos" in t              # marca del recorte


def test_prompt_exige_no_dejar_dimensiones_vacias():
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    assert "vacia" in system.lower()


def test_prompt_prohibe_inventar_contexto():
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    low = system.lower()
    assert "no inventes" in low
    assert "explicit" in low  # "explicito/s en los mensajes"


def test_prompt_reconoce_respuesta_implicita():
    # La respuesta al motivo puede estar embebida en el texto del agente (aunque sea
    # promocional): el modelo debe leerla y no marcar "no respondio" si esta presente.
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    low = system.lower()
    assert "implicita" in low or "contenida en lo que dijo el agente" in low
    assert "informacion pedida" in low or "informacion pedida esta presente" in low


def test_prompt_pide_la_forma_json_de_salida():
    system, _ = build_scorer_prompt("bot", MSGS_HUMAN, thread_context="")
    assert "UNICAMENTE" in system
    assert '"rating_label"' in system
    assert '"rating_rationale"' in system
    assert "cobertura_info" in system          # dims de bot en la forma


def test_schema_de_salida_depende_de_la_rubrica():
    sch = build_output_schema("bot")
    props = sch["properties"]["dimensions"]["properties"]
    assert "cobertura_info" in props and "errores" in props
    assert sch["properties"]["rating_label"]["enum"] == [
        "optima", "funcional", "mejorable", "deficiente", "falla",
    ]
    # el LLM NO emite stars: esa se calcula aparte
    assert "stars" not in sch["properties"]


def test_schema_incluye_atencion_y_deposit_observed_en_properties_no_required():
    # PIEZA 3 — pase unificado: el schema suma atencion (pasividad portada) y la
    # observacion de deposito en properties (el grammar los pide), pero NO en required:
    # son best-effort, no deben hacer fallar un rating por lo demas valido.
    sch = build_output_schema("human")
    props = sch["properties"]
    assert props["atencion"]["enum"] == ["empujo", "pasivo", "no_respondio"]
    assert props["deposit_observed"]["type"] == "boolean"
    assert "atencion" not in sch["required"]
    assert "deposit_observed" not in sch["required"]
    assert set(sch["required"]) == {"dimensions", "rating_label", "rating_rationale"}


def test_prompt_pide_atencion_y_deposit_observed_en_la_forma_json():
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    assert '"atencion"' in system
    assert '"deposit_observed"' in system
    assert "empujo|pasivo|no_respondio" in system


def test_prompt_de_motivo_porta_reglas_de_dos_capas():
    # Una rubrica de MOTIVO (uplift set) suma el modelo de 2 capas: piso 'aceptable'
    # si atendio el motivo (aunque templateado), uplift para superar aceptable.
    system, _ = build_scorer_prompt("deposito", MSGS_HUMAN, thread_context="")
    low = system.lower()
    assert "piso" in low
    assert "aceptable" in low
    assert "uplift" in low
    assert "plantilla no" in low or "templateado" in low  # templateado != peor nota


def test_prompt_legacy_human_no_incluye_dos_capas():
    # human/bot (sin uplift) NO llevan las reglas de 2 capas: comportamiento previo.
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    assert "UPLIFT" not in system


def test_transcript_motivo_rotula_negocio_como_agente():
    # format_transcript no debe romper con una rubrica de motivo (no esta en
    # _BUSINESS_LABEL); rotula el lado negocio como 'Agente'.
    t = format_transcript(MSGS_HUMAN, "deposito")
    assert "Agente:" in t and "Cliente:" in t


def test_prompt_porta_las_reglas_de_atencion_del_operador():
    system, _ = build_scorer_prompt("human", MSGS_HUMAN, thread_context="")
    low = system.lower()
    # solo juzga al operador humano y describe las tres etiquetas
    assert "operador" in low
    assert "empujo" in low and "pasivo" in low and "no_respondio" in low
    # deposit_observed es OBSERVACION, no decision (el gate determinista manda)
    assert "determinista" in low
