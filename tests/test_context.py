"""Tests del digest de contexto del hilo (parte pura, sin DB)."""
from datetime import datetime, timedelta, timezone

from src.context import MAX_THREAD_VISITS, format_thread_digest

BASE = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _visit(i, is_bot, msg):
    return {"created_at": BASE + timedelta(hours=i), "is_bot": is_bot, "first_customer_msg": msg}


def test_digest_vacio_si_no_hay_visitas():
    assert format_thread_digest([]) == ""


def test_digest_rotula_bot_y_agente_y_muestra_cliente():
    d = format_thread_digest([_visit(1, True, "hola"), _visit(2, False, "gracias")])
    assert "[BOT]" in d
    assert "[AGENTE]" in d
    assert "hola" in d


def test_digest_capa_a_las_ultimas_n_visitas():
    visits = [_visit(i, False, f"m{i}") for i in range(30)]
    d = format_thread_digest(visits)
    lineas = [l for l in d.splitlines() if l.startswith("- ")]
    assert len(lineas) == MAX_THREAD_VISITS       # se cap
    assert "m29" in d                             # conserva las mas recientes
    assert "m0" not in d                          # descarta las viejas
    assert "omitidas" in d                        # marca lo omitido


def test_digest_sin_mensaje_de_cliente_se_marca():
    d = format_thread_digest([_visit(1, False, None)])
    assert "sin mensaje de cliente" in d
