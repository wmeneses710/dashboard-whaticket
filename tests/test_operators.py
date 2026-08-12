

# --- QUINTA PUERTA: EL NOMBRE VIVE EN LAS NOTAS DEL CRM ------------------------------
# El negocio seguia viendo "Operador sin identificar". Al abrir el objeto crudo aparecio que
# el ETL SI guarda el nombre, en las notas internas que ya parseamos para cortar interacciones:
#     *Asignado automáticamente* a Michelle
#     Michelle *resuelto* la conversación
#     Anya Alexandra *resuelto* la conversación
# Usabamos la frontera y tirabamos el nombre. En esa conversacion `conversations.user_id`,
# `tickets.user_id` y `messages.user_id` son TODOS NULL y no hay firma `*Nombre:*`.
#
# MEDIDO el 2026-08-12: de 127.898 sesiones con al menos un mensaje humano del negocio, 881
# no tienen ni user_id ni firma -- y **880 (100%) tienen el nombre en una nota**.
# PRECISION validada contra la verdad conocida (las sesiones con UNA firma clara en el cuerpo):
#     nota *resuelto*  presente en 104.301 sesiones, el ultimo nombre acierta el 99%
#     nota *aceptado*  presente en   5.765,                                      98%
#     nota *asignado*  presente en  95.893,                                      98%
# Se prefiere `*resuelto*` por precision Y por volumen. Y el nombre pasa por el MISMO guard
# `es_nombre_de_persona` que la firma: sin el, "Gerente de Cuentas" (28 sesiones) entraria
# como si fuera una persona.

def test_el_nombre_sale_de_la_nota_de_cierre():
    from src.operators import nombre_de_notas
    msgs = [{"from_me": True, "is_note": True, "body": "Michelle *resuelto* la conversación"}]
    assert nombre_de_notas(msgs) == "Michelle"


def test_gana_el_ULTIMO_cierre_porque_es_quien_cerro_esa_visita():
    # Una conversacion reabierta tiene varios cierres y NO son la misma persona. Con el
    # ventaneo por interaccion se llama con la ventana juzgada, asi que el ultimo de ESA
    # ventana es quien la atendio.
    from src.operators import nombre_de_notas
    msgs = [{"from_me": True, "is_note": True, "body": "Michelle *resuelto* la conversación"},
            {"from_me": True, "is_note": True, "body": "Anya Alexandra *resuelto* la conversación"}]
    assert nombre_de_notas(msgs) == "Anya Alexandra"


def test_el_cierre_le_gana_al_aceptado_y_al_asignado():
    from src.operators import nombre_de_notas
    msgs = [{"from_me": True, "is_note": True, "body": "*Asignado automáticamente* a Pedro"},
            {"from_me": True, "is_note": True, "body": "Lucia *aceptado* la conversación"},
            {"from_me": True, "is_note": True, "body": "Ana *resuelto* la conversación"}]
    assert nombre_de_notas(msgs) == "Ana"
    # sin cierre, manda el aceptado
    assert nombre_de_notas(msgs[:2]) == "Lucia"
    # y si solo hay asignacion, esa
    assert nombre_de_notas(msgs[:1]) == "Pedro"


def test_un_ROL_no_es_una_persona():
    # Mismo guard que la firma: sin el, "Gerente de Cuentas" entraria como operador.
    from src.operators import nombre_de_notas
    assert nombre_de_notas(
        [{"from_me": True, "is_note": True,
          "body": "Gerente de Cuentas *resuelto* la conversación"}]) is None


def test_los_mensajes_normales_no_son_notas():
    # Solo las NOTAS internas del CRM. Un cliente escribiendo "Ana *resuelto*" no nombra a nadie.
    from src.operators import nombre_de_notas
    assert nombre_de_notas(
        [{"from_me": False, "is_note": False, "body": "Ana *resuelto* la conversación"}]) is None
    assert nombre_de_notas([]) is None
