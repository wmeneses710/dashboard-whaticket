"""Tests de src/atribucion.py: integridad de la atribucion de messages.conversation_id.

Todo PURO, en memoria. La pregunta que contestan estas funciones es una sola: cuando la
sesionizacion decide con timestamps de MENSAJES (first_at/last_at), esos timestamps
pertenecen de verdad al episodio al que estan atribuidos, o son mensajes que el ETL
metio en la conversacion equivocada?
"""
from datetime import datetime, timedelta

from src.atribucion import (
    ANTES,
    DESPUES,
    Frontera,
    comparar_reglas,
    con_piso_de_mensajes,
    desvio_de_ventana,
    es_flip_de_gap,
    es_sucia,
    filtrar_tickets_completos,
    fronteras_de_ticket,
    ordenar_episodios,
    particion_por_sesion,
    resumen,
    sin_actividad,
)

BASE = datetime(2026, 1, 1, 8, 0, 0)
TOL = timedelta(minutes=5)


def _t(hours):
    return BASE + timedelta(hours=hours)


# --- desvio_de_ventana: el timestamp cae dentro de la ventana propia del episodio? ---

def test_dentro_de_la_ventana_no_hay_desvio():
    assert desvio_de_ventana(_t(2), _t(0), _t(4), TOL) is None


def test_anterior_al_nacimiento_es_desvio_hacia_antes():
    d = desvio_de_ventana(_t(-48), _t(0), _t(4), TOL)
    assert d is not None
    assert d.direccion == ANTES
    assert d.magnitud == timedelta(hours=48)


def test_anterior_pero_dentro_de_la_tolerancia_no_es_desvio():
    # El primer mensaje puede llegar unos segundos antes de que se inserte la fila de
    # la conversacion: eso es normal, no es mala atribucion.
    assert desvio_de_ventana(_t(0) - timedelta(minutes=2), _t(0), _t(4), TOL) is None


def test_posterior_a_la_resolucion_es_desvio_hacia_despues():
    d = desvio_de_ventana(_t(30), _t(0), _t(4), TOL)
    assert d is not None
    assert d.direccion == DESPUES
    assert d.magnitud == timedelta(hours=26)


def test_posterior_pero_dentro_de_la_tolerancia_no_es_desvio():
    assert desvio_de_ventana(_t(4) + timedelta(minutes=2), _t(0), _t(4), TOL) is None


def test_conversacion_sin_resolver_no_tiene_techo():
    # resuelto_en None = episodio abierto: cualquier mensaje posterior es legitimo.
    assert desvio_de_ventana(_t(9999), _t(0), None, TOL) is None


def test_timestamp_nulo_no_se_juzga():
    # Episodio sin mensajes reales: el gap cae a created_at por el fallback de
    # _actividad, no hay timestamp de mensaje que auditar.
    assert desvio_de_ventana(None, _t(0), _t(4), TOL) is None


# --- es_flip_de_gap: el termino gap cambio la decision de esta frontera? ------------

def _f(gap_viejo_h, gap_nuevo_h, prev_cerro=False, cambio_operador=False,
       desvio_prev=None, desvio_ep=None):
    return Frontera(
        ticket_id="t", prev_id="a", ep_id="b",
        gap_viejo=timedelta(hours=gap_viejo_h), gap_nuevo=timedelta(hours=gap_nuevo_h),
        prev_cerro=prev_cerro, cambio_operador=cambio_operador,
        desvio_prev=desvio_prev, desvio_ep=desvio_ep,
    )


def test_sin_cambio_de_decision_no_es_flip():
    # Las dos reglas cortan (ambos gaps > 5h) -> el cambio de medicion no movio nada.
    assert es_flip_de_gap(_f(8, 9)) is False


def test_corta_a_mergea_es_flip():
    # La vieja cortaba (nacimientos a 8h) y la nueva mergea (silencio real 1h).
    assert es_flip_de_gap(_f(8, 1)) is True


def test_mergea_a_corta_es_flip():
    assert es_flip_de_gap(_f(1, 8)) is True


def test_cierre_del_previo_anula_el_termino_gap():
    # Si el previo CERRO, la frontera corta con cualquier gap: el termino gap no decide.
    assert es_flip_de_gap(_f(8, 1, prev_cerro=True)) is False


def test_cambio_de_operador_anula_el_termino_gap():
    assert es_flip_de_gap(_f(8, 1, cambio_operador=True)) is False


def test_silencio_negativo_mergea():
    # El previo seguia activo cuando arranco el siguiente -> silencio negativo.
    assert es_flip_de_gap(_f(8, -3)) is True


# --- es_sucia: el flip se apoya en timestamps mal atribuidos? -----------------------

def _desvio(direccion, horas):
    from src.atribucion import Desvio
    return Desvio(direccion=direccion, magnitud=timedelta(hours=horas))


def test_limpia_cuando_los_dos_timestamps_estan_en_ventana():
    assert es_sucia(_f(8, 1)) is False


def test_sucia_cuando_el_last_at_del_previo_se_fue_al_futuro():
    # Este es el caso que infla merges: el previo absorbio mensajes posteriores a su
    # resolucion -> su last_at queda adelantado -> silencio negativo -> mergea.
    assert es_sucia(_f(8, -3, desvio_prev=_desvio(DESPUES, 200))) is True


def test_sucia_cuando_el_first_at_del_siguiente_es_anterior_a_su_nacimiento():
    assert es_sucia(_f(1, 8, desvio_ep=_desvio(ANTES, 1100 * 24))) is True


# --- resumen: los dos baldes ------------------------------------------------------

def test_resumen_separa_limpios_de_sucios():
    fronteras = [
        _f(8, 1),                                              # flip limpio
        _f(8, 1),                                              # flip limpio
        _f(8, -3, desvio_prev=_desvio(DESPUES, 200)),          # flip sucio
        _f(9, 8),                                              # no es flip
        _f(8, 1, prev_cerro=True),                             # no decide el gap
    ]
    r = resumen(fronteras)
    assert r.fronteras == 5
    assert r.flips == 3
    assert r.limpios == 2
    assert r.sucios == 1


def test_resumen_cuenta_la_direccion_del_flip():
    fronteras = [_f(8, 1), _f(8, 1), _f(1, 8)]
    r = resumen(fronteras)
    assert r.corta_a_mergea == 2
    assert r.mergea_a_corta == 1


def test_resumen_vacio_no_divide_por_cero():
    r = resumen([])
    assert r.flips == 0
    assert r.pct_sucios == 0.0


def test_pct_sucios_se_calcula_sobre_los_flips_no_sobre_las_fronteras():
    # 1 sucio de 2 flips = 50%, aunque haya 10 fronteras en total.
    fronteras = [_f(8, 1), _f(8, -3, desvio_prev=_desvio(DESPUES, 9))]
    fronteras += [_f(9, 8)] * 8
    r = resumen(fronteras)
    assert r.flips == 2
    assert r.pct_sucios == 50.0


# --- armado de fronteras y comparacion de las dos reglas --------------------------

def _epi(cid, creado, resuelto=None, first=None, last=None, body=None, op="op1"):
    """Episodio como lo arma refresh_account_sessions, + resolved_at. Horas desde BASE."""
    return {"conversation_id": cid, "created_at": _t(creado),
            "resolved_at": _t(resuelto) if resuelto is not None else None,
            "first_at": _t(first) if first is not None else None,
            "last_at": _t(last) if last is not None else None,
            "last_operator_body": body, "operator_id": op}


def test_sin_actividad_saca_la_ventana_para_reproducir_la_regla_vieja():
    eps = [_epi("a", 0, 2, 0, 2)]
    out = sin_actividad(eps)
    assert "first_at" not in out[0] and "last_at" not in out[0]
    # No muta el original: las dos reglas corren sobre los mismos episodios.
    assert eps[0]["first_at"] == _t(0)


def test_ordenar_episodios_desempata_por_id_como_assign_sessions():
    eps = [_epi("b", 0), _epi("a", 0)]
    assert [e["conversation_id"] for e in ordenar_episodios(eps)] == ["a", "b"]


def test_fronteras_una_por_par_consecutivo():
    eps = [_epi("a", 0, 2, 0, 2), _epi("b", 4, 6, 4, 6), _epi("c", 8, 10, 8, 10)]
    fs = fronteras_de_ticket("t", eps, TOL)
    assert [(f.prev_id, f.ep_id) for f in fs] == [("a", "b"), ("b", "c")]


def test_frontera_mide_los_dos_gaps():
    # nacimientos a 8h; actividad: previo termina a 7h, siguiente arranca a 8h -> 1h.
    fs = fronteras_de_ticket("t", [_epi("a", 0, 7, 0, 7), _epi("b", 8, 10, 8, 10)], TOL)
    assert fs[0].gap_viejo == timedelta(hours=8)
    assert fs[0].gap_nuevo == timedelta(hours=1)


def test_frontera_sin_ventana_de_actividad_cae_a_created_at():
    # Episodio sin mensajes reales: los dos gaps coinciden, y no hay desvio que auditar.
    fs = fronteras_de_ticket("t", [_epi("a", 0, 2), _epi("b", 8, 10)], TOL)
    assert fs[0].gap_nuevo == fs[0].gap_viejo == timedelta(hours=8)
    assert fs[0].desvio_prev is None and fs[0].desvio_ep is None


def test_frontera_detecta_el_last_at_del_previo_fuera_de_su_ventana():
    # El previo resolvio a las 2h pero tiene mensajes hasta las 400h: mala atribucion.
    fs = fronteras_de_ticket("t", [_epi("a", 0, 2, 0, 400), _epi("b", 20, 22, 20, 22)], TOL)
    assert fs[0].desvio_prev is not None
    assert fs[0].desvio_prev.direccion == DESPUES
    assert fs[0].desvio_ep is None
    # EL TECHO NEUTRALIZA EL EFECTO. Sin el, last_at=400h daba silencio NEGATIVO (-380h)
    # y la frontera mergeaba dos interacciones separadas. Con techo en resolved_at (2h),
    # el silencio son las 18h reales -> corta. El desvio se sigue reportando: el dato
    # esta mal atribuido igual, solo que ya no decide la frontera.
    assert fs[0].gap_nuevo == timedelta(hours=18)


def test_frontera_marca_cierre_y_cambio_de_operador():
    eps = [_epi("a", 0, 2, 0, 2, body="ya le cargue el saldo, exitos", op="op1"),
           _epi("b", 4, 6, 4, 6, op="op2")]
    f = fronteras_de_ticket("t", eps, TOL)[0]
    assert f.prev_cerro is True
    assert f.cambio_operador is True


def test_particion_agrupa_los_episodios_de_cada_sesion():
    asignacion = [
        {"conversation_id": "a", "session_id": "a"},
        {"conversation_id": "b", "session_id": "a"},
        {"conversation_id": "c", "session_id": "c"},
    ]
    p = particion_por_sesion(asignacion)
    assert p["a"] == p["b"] == frozenset({"a", "b"})
    assert p["c"] == frozenset({"c"})


def test_comparar_reglas_cuenta_el_merge_que_la_regla_nueva_agrega():
    # Un ticket donde la vieja corta (nacimientos a 8h) y la nueva mergea (silencio 1h).
    by_ticket = {"t": [_epi("a", 0, 7, 0, 7, body="hola"), _epi("b", 8, 10, 8, 10)]}
    c = comparar_reglas(by_ticket, TOL)
    assert c.tickets == 1
    assert c.episodios == 2
    assert c.sesiones_vieja == 2
    assert c.sesiones_nueva == 1
    # 'b' pasa a la sesion de 'a' -> cambia su session_id.
    assert c.cambio_session_id == 1
    # Los DOS episodios quedan en una sesion recompuesta (a gano un companiero).
    assert c.sesion_recompuesta == 2
    assert len(c.fronteras) == 1


def test_comparar_reglas_no_cuenta_nada_cuando_las_dos_reglas_coinciden():
    by_ticket = {"t": [_epi("a", 0, 1, 0, 1, body="hola"), _epi("b", 50, 51, 50, 51)]}
    c = comparar_reglas(by_ticket, TOL)
    assert c.sesiones_vieja == c.sesiones_nueva == 2
    assert c.cambio_session_id == 0
    assert c.sesion_recompuesta == 0


def test_comparar_reglas_ignora_tickets_de_un_solo_episodio_para_fronteras():
    by_ticket = {"t1": [_epi("a", 0, 1, 0, 1)],
                 "t2": [_epi("b", 0, 1, 0, 1), _epi("c", 2, 3, 2, 3)]}
    c = comparar_reglas(by_ticket, TOL)
    assert c.episodios == 3
    assert len(c.fronteras) == 1


# --- ventana de fechas: comparar poblacion sanada vs sin sanar ---------------------

def test_ventana_abierta_no_filtra_nada():
    por_ticket = {"t": [_epi("a", 0), _epi("b", 2)]}
    f = filtrar_tickets_completos(por_ticket, None, None)
    assert f.tickets == por_ticket
    assert f.tickets_excluidos == 0


def test_ticket_entero_dentro_de_la_ventana_entra():
    por_ticket = {"t": [_epi("a", 1), _epi("b", 3)]}
    f = filtrar_tickets_completos(por_ticket, _t(0), _t(5))
    assert list(f.tickets) == ["t"]
    assert f.tickets_excluidos == 0


def test_ticket_entero_fuera_de_la_ventana_se_excluye():
    por_ticket = {"t": [_epi("a", 100), _epi("b", 102)]}
    f = filtrar_tickets_completos(por_ticket, _t(0), _t(5))
    assert f.tickets == {}
    assert f.tickets_excluidos == 1
    assert f.episodios_excluidos == 2


def test_ticket_a_caballo_del_borde_se_excluye_entero():
    # Si se recortaran los episodios de afuera, el primer episodio que queda pareceria
    # inicio de sesion y fabricaria una frontera que no existe. Se excluye el ticket
    # COMPLETO: la unidad de analisis es el ticket, no el episodio.
    por_ticket = {"t": [_epi("a", -10), _epi("b", 2)]}
    f = filtrar_tickets_completos(por_ticket, _t(0), _t(5))
    assert f.tickets == {}
    assert f.tickets_excluidos == 1


def test_solo_desde_deja_el_techo_abierto():
    por_ticket = {"viejo": [_epi("a", -10)], "nuevo": [_epi("b", 10)]}
    f = filtrar_tickets_completos(por_ticket, _t(0), None)
    assert list(f.tickets) == ["nuevo"]


def test_solo_hasta_deja_el_piso_abierto():
    por_ticket = {"viejo": [_epi("a", -10)], "nuevo": [_epi("b", 10)]}
    f = filtrar_tickets_completos(por_ticket, None, _t(0))
    assert list(f.tickets) == ["viejo"]


def test_bordes_de_la_ventana_son_inclusivos():
    por_ticket = {"t": [_epi("a", 0), _epi("b", 5)]}
    f = filtrar_tickets_completos(por_ticket, _t(0), _t(5))
    assert list(f.tickets) == ["t"]


# --- piso de mensajes: simular el mundo post-recorte ------------------------------

def test_piso_de_mensajes_se_inyecta_despues_del_filtro_de_cuenta():
    sql = "SELECT x FROM messages\n WHERE account = %(account)s AND is_note = false\n"
    out = con_piso_de_mensajes(sql)
    assert "created_at >= COALESCE(%(msgs_desde)s" in out
    # El resto de la clausula sobrevive intacto.
    assert "is_note = false" in out
    assert out.count("account = %(account)s") == 1


def test_piso_de_mensajes_falla_fuerte_si_no_encuentra_el_ancla():
    # Si algun dia cambia el WHERE de src/sessions.py, esto tiene que romper con un
    # error claro en vez de devolver un SQL sin piso (que mediria el mundo equivocado
    # sin avisar).
    import pytest
    with pytest.raises(ValueError, match="ancla"):
        con_piso_de_mensajes("SELECT x FROM messages WHERE cuenta = 'otra'")


def test_las_tres_queries_de_mensajes_de_sessions_aceptan_el_piso():
    # Contrato con src/sessions.py: las tres traen mensajes y las tres tienen el ancla.
    from src.sessions import _LAST_AGENT_SQL, _LAST_MSG_SQL, _PRIMARY_AGENT_SQL
    for sql in (_LAST_AGENT_SQL, _PRIMARY_AGENT_SQL, _LAST_MSG_SQL):
        assert "created_at >= COALESCE(%(msgs_desde)s" in con_piso_de_mensajes(sql)


# --- radio de impacto vs calidad del fix ------------------------------------------

def test_resumen_expone_los_sucios_sobre_el_total_de_fronteras():
    # 1 sucio de 2 flips = 50% sobre flips, pero 10% sobre las 10 fronteras. Las dos
    # cifras contestan preguntas distintas y el go/no-go usa la segunda.
    fronteras = [_f(8, 1), _f(8, -3, desvio_prev=_desvio(DESPUES, 9))]
    fronteras += [_f(9, 8)] * 8
    r = resumen(fronteras)
    assert r.pct_sucios == 50.0
    assert r.pct_sucios_sobre_fronteras == 10.0


def test_pct_sucios_sobre_fronteras_vacio_no_divide_por_cero():
    assert resumen([]).pct_sucios_sobre_fronteras == 0.0


# --- la frontera mide la regla nueva REAL, con el techo de resolved_at -------------

def test_gap_nuevo_de_la_frontera_aplica_el_techo_de_resolved_at():
    # Sin techo el silencio seria 0 (el mensaje archivado en el previo cierra el hueco);
    # con techo son las 7h reales. Si la Frontera no aplicara el techo, el simulador
    # medirian una regla que ya no es la del codigo.
    fs = fronteras_de_ticket("t", [_epi("a", 0, 1, 0, 8), _epi("b", 8, 9, 8, 9)], TOL)
    assert fs[0].gap_nuevo == timedelta(hours=7)
    assert fs[0].gap_viejo == timedelta(hours=8)
    # Sigue marcandose el desvio: el dato ESTA mal atribuido aunque el techo lo neutralice.
    assert fs[0].desvio_prev is not None
