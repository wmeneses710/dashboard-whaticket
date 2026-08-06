"""Tests del script de re-materializacion de sesiones (scripts/rematerializar_sesiones.py).

La logica PURA vive en src/rematerializar.py para poder testearla: `pytest.ini` no colecta
scripts/. Lo que se valida aca es lo que puede arruinar una corrida:
  - el guard de copia (el script ESCRIBE: nunca debe tocar produccion por accidente)
  - el advisory lock compartido con el worker (si no, deadlock en conversation_sessions)
  - el reporte antes/despues
"""
import pytest

from src.rematerializar import (
    LOCK_KEY,
    PISTAS_DE_COPIA,
    Resultado,
    es_copia,
    formatear_reporte,
    rematerializar,
)


# --- guard de copia: el script ESCRIBE, asi que este guard es la red de seguridad ---

def test_reconoce_los_nombres_de_copia():
    for nombre in ("whaticket_copia", "whaticket_copy", "wha_local", "db_test",
                   "algo_dev", "x_stage"):
        assert es_copia(nombre) is True, nombre


def test_rechaza_produccion():
    for nombre in ("whaticket", "produccion", "prod_db", "main"):
        assert es_copia(nombre) is False, nombre


def test_el_guard_no_depende_de_mayusculas():
    assert es_copia("WHATICKET_COPIA") is True


def test_las_pistas_son_las_MISMAS_que_snapshot_scores():
    # Los dos scripts destructivos tienen que compartir criterio: si uno acepta una base
    # y el otro no, el operador se confunde en el peor momento.
    assert PISTAS_DE_COPIA == ("copia", "copy", "local", "test", "dev", "stage")


def test_el_lock_es_el_MISMO_del_worker():
    # Si difiere, el script y el worker escriben conversation_sessions en paralelo y
    # deadlockean (el bug del commit 3a9e337).
    from src.worker import _SCORING_LOCK_KEY
    assert LOCK_KEY == _SCORING_LOCK_KEY


# --- rematerializar: orquesta, cuenta y reporta -----------------------------------

class _CursorFalso:
    """Cursor que responde los COUNT y registra lo ejecutado. No ejecuta SQL."""

    def __init__(self, sesiones_antes=100, mapa_antes=250, sesiones_despues=97,
                 mapa_despues=250):
        self.ejecutado = []
        self._counts = [sesiones_antes, mapa_antes, sesiones_despues, mapa_despues]
        self._i = 0

    def execute(self, q, p=None):
        self.ejecutado.append((q, p))

    def fetchone(self):
        v = self._counts[min(self._i, len(self._counts) - 1)]
        self._i += 1
        return (v,)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_rematerializar_devuelve_el_antes_y_el_despues(monkeypatch):
    import src.rematerializar as mod
    monkeypatch.setattr(mod, "refresh_account_sessions", lambda cur, cuenta: 97)
    cur = _CursorFalso(sesiones_antes=100, mapa_antes=250,
                       sesiones_despues=97, mapa_despues=250)
    r = rematerializar(cur, "sistemas")
    assert isinstance(r, Resultado)
    assert r.cuenta == "sistemas"
    assert r.sesiones_antes == 100
    assert r.sesiones_despues == 97
    assert r.materializadas == 97


def test_rematerializar_llama_a_la_funcion_REAL_de_sesionizacion(monkeypatch):
    # El script NO reimplementa la regla: delega en src.sessions. Si algun dia cambia
    # la regla, el script la sigue sin tocarse.
    import src.rematerializar as mod
    visto = {}

    def espia(cur, cuenta):
        visto["cuenta"] = cuenta
        return 42

    monkeypatch.setattr(mod, "refresh_account_sessions", espia)
    rematerializar(_CursorFalso(), "datos")
    assert visto["cuenta"] == "datos"


def test_dry_run_NO_llama_a_la_sesionizacion(monkeypatch):
    import src.rematerializar as mod

    def boom(cur, cuenta):
        raise AssertionError("en dry-run no se debe escribir")

    monkeypatch.setattr(mod, "refresh_account_sessions", boom)
    r = rematerializar(_CursorFalso(), "sistemas", dry_run=True)
    assert r.materializadas is None
    assert r.sesiones_despues is None


# --- reporte ----------------------------------------------------------------------

def test_el_reporte_muestra_el_delta():
    r = Resultado(cuenta="sistemas", sesiones_antes=111901, sesiones_despues=111581,
                  mapa_antes=136900, mapa_despues=136900, materializadas=111581)
    texto = formatear_reporte([r])
    assert "sistemas" in texto
    assert "111,901" in texto and "111,581" in texto
    assert "-320" in texto, "hay que ver el delta, no solo los dos numeros"


def test_el_reporte_de_dry_run_dice_que_no_escribio():
    r = Resultado(cuenta="datos", sesiones_antes=15750, sesiones_despues=None,
                  mapa_antes=16056, mapa_despues=None, materializadas=None)
    texto = formatear_reporte([r], dry_run=True)
    assert "dry-run" in texto.lower()
    assert "15,750" in texto


def test_el_reporte_no_revienta_sin_cuentas():
    assert isinstance(formatear_reporte([]), str)
