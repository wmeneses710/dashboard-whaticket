"""El respaldo cuando `tickets.is_group` llega NULL: el JID del grupo viene en el numero.

POR QUE HACE FALTA. El skip `grupo_de_whatsapp` (2026-08-24) depende de `tickets.is_group`,
y esa marca no siempre esta. Medido sobre las 17.041 sesiones pendientes SIN COLA de la
copia:
      608  `is_group = true`   -> ya las agarra el gate
   16.168  `is_group` NULL     -> de esas, 214 tienen numero de grupo
El resto (13.052) no tiene contacto ninguno y de esas no se puede saber nada -- este respaldo
no las toca, y falla del lado seguro.

LA SEÑAL ES EXACTA, no una heuristica. WhatsApp identifica un grupo con un JID que viaja en
`contacts.number` (ej. `120363217408052038`, 18 digitos que arrancan con 120363). Medido en
la copia sobre 93.705 conversaciones con ticket:
    is_group = true    5.060 -> 5.060 con numero de 18-23 digitos   (100% cubierto)
    is_group = false  88.645 ->     0 con numero de mas de 15       (0 falsos positivos)
El umbral cae en un hueco limpio: las personas van de 7 a 13 digitos, los grupos de 18 a 23.
Nadie vive entre 14 y 17.
"""
from src.segments import LARGO_MAXIMO_DE_PERSONA, es_grupo_de_whatsapp


def test_la_marca_del_crm_manda_cuando_esta():
    """Si el CRM lo dice, no se adivina: la marca la pone WhatsApp."""
    assert es_grupo_de_whatsapp(True, "0987654321") is True
    assert es_grupo_de_whatsapp(False, "120363217408052038") is False


def test_sin_marca_el_numero_largo_delata_al_grupo():
    assert es_grupo_de_whatsapp(None, "120363217408052038") is True


def test_sin_marca_un_numero_de_persona_no_es_grupo():
    for numero in ("0987654321", "593987654321", "12039968604", "1234567"):
        assert es_grupo_de_whatsapp(None, numero) is False, numero


def test_sin_marca_y_sin_numero_NO_se_saltea_nada():
    """Falla del lado seguro: 13.052 sesiones pendientes no tienen contacto, y saltearlas
    por las dudas seria borrarlas del padron sin evidencia."""
    assert es_grupo_de_whatsapp(None, None) is False
    assert es_grupo_de_whatsapp(None, "") is False
    assert es_grupo_de_whatsapp(None, "   ") is False


def test_el_umbral_cae_en_el_hueco_medido():
    """Las personas llegan a 13 digitos y los grupos arrancan en 18. El umbral tiene que
    quedar en el medio, no pegado a ninguno de los dos bordes."""
    assert LARGO_MAXIMO_DE_PERSONA == 15
    assert es_grupo_de_whatsapp(None, "1" * 13) is False
    assert es_grupo_de_whatsapp(None, "1" * 15) is False
    assert es_grupo_de_whatsapp(None, "1" * 16) is True
    assert es_grupo_de_whatsapp(None, "1" * 23) is True


def test_el_jid_viejo_con_guion_tambien_entra_por_largo():
    """Los grupos antiguos usan `telefono-timestamp`. No hace falta una regla propia: 23
    caracteres ya pasan el umbral. (255 de los grupos de la copia tienen guion.)"""
    assert es_grupo_de_whatsapp(None, "593999999999-1420000000") is True


def test_los_espacios_no_cuentan_como_digitos():
    """Un numero formateado no puede volverse grupo por los separadores."""
    assert es_grupo_de_whatsapp(None, "+593 98 765 4321") is False


# --- el gate lo usa de verdad ---------------------------------------------------------

def test_el_worker_saltea_un_grupo_que_solo_se_delata_por_el_numero(monkeypatch):
    import src.worker as worker
    from tests.test_worker import (  # noqa: PLC0415
        _CtxConn,
        _session_row,
        _T0,
    )

    monkeypatch.setattr(worker, "fetch_session_messages", lambda cur, sid: [
        {"created_at": _T0, "from_me": False, "is_note": False,
         "body": "Un excelente día, empezamos con unos picos gratis por aquí",
         "sent_from": None, "user_id": None, "media_type": None},
    ])
    conn = _CtxConn()
    row = _session_row()
    row["is_group"] = None                       # el CRM no lo marco
    row["contact_number"] = "120363217408052038"  # pero el JID lo delata
    eval_status, skip_reason, score = worker.score_session_and_store(
        conn, row, llm=None, op_map={})
    assert (eval_status, skip_reason) == ("skipped", "grupo_de_whatsapp")
    assert score is None


def test_el_sql_de_pendientes_trae_el_numero_del_contacto():
    from tests.test_worker import _FakeCursor  # noqa: PLC0415

    cur = _FakeCursor([], description=[])
    worker_mod = __import__("src.worker", fromlist=["fetch_pending_sessions"])
    worker_mod.fetch_pending_sessions(cur, "datos", 30)
    query, _ = cur.executed[0]
    assert "contact_number" in query, "sin el numero el respaldo nunca dispara"
    assert "LEFT JOIN contacts" in query, (
        "el contacto se alcanza por tickets.contact_id, y tiene que ser LEFT: 13.052 "
        "sesiones pendientes no tienen contacto")
