"""Re-materializacion de sesiones a demanda (logica pura + orquestacion, sin CLI).

POR QUE EXISTE. `refresh_account_sessions` se llamaba UNICAMENTE desde
`src/worker.py`: en el arranque del loop y periodicamente. Eso obligaba a arrancar el
worker para re-materializar, y el worker ADEMAS empieza a scorear. No se podian separar
los pasos, y el orden importa:

    1. recortar mensajes viejos    (si no, el start_at queda en el mes equivocado)
    2. RE-MATERIALIZAR sesiones    <- este paso
    3. snapshot_scores --guardar --vaciar
    4. re-scorear

Si se arranca el worker antes del paso 3, scorea con la tabla vieja llena y el snapshot
posterior congela una mezcla de dos lineas base.

NO REIMPLEMENTA LA REGLA: delega en `refresh_account_sessions` (src/sessions.py). Si la
regla cambia, esto la sigue sin tocarse.

El CLI vive en scripts/rematerializar_sesiones.py; aca queda lo testeable.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.sessions import ensure_sessions_table, refresh_account_sessions

# MISMO lock que el worker (src/worker._SCORING_LOCK_KEY). Sin esto, el script y el
# worker escriben `conversation_sessions` en paralelo y deadlockean: es exactamente el
# bug del commit 3a9e337. El test `test_el_lock_es_el_MISMO_del_worker` lo fija.
LOCK_KEY = 823147

# MISMAS pistas que scripts/snapshot_scores.py. Los dos scripts ESCRIBEN, asi que tienen
# que compartir criterio: si uno acepta una base y el otro no, el operador se confunde en
# el peor momento.
PISTAS_DE_COPIA = ("copia", "copy", "local", "test", "dev", "stage")

_COUNT_SESIONES = "SELECT count(*) FROM conversation_sessions WHERE account = %s"
_COUNT_MAPA = "SELECT count(*) FROM conversation_session_map WHERE account = %s"


@dataclass(frozen=True)
class Resultado:
    """Antes/despues de una cuenta. Los campos `despues` son None en dry-run."""
    cuenta: str
    sesiones_antes: int
    sesiones_despues: int | None
    mapa_antes: int
    mapa_despues: int | None
    materializadas: int | None


def es_copia(nombre_bd: str) -> bool:
    """El nombre de la base parece una copia y no produccion."""
    return any(p in (nombre_bd or "").lower() for p in PISTAS_DE_COPIA)


def rematerializar(cur, cuenta: str, dry_run: bool = False) -> Resultado:
    """Recomputa y materializa las sesiones de UNA cuenta. Devuelve el antes/despues.

    dry_run: solo cuenta lo que hay, sin tocar nada.
    """
    cur.execute(_COUNT_SESIONES, (cuenta,))
    sesiones_antes = cur.fetchone()[0]
    cur.execute(_COUNT_MAPA, (cuenta,))
    mapa_antes = cur.fetchone()[0]

    if dry_run:
        return Resultado(cuenta=cuenta, sesiones_antes=sesiones_antes,
                         sesiones_despues=None, mapa_antes=mapa_antes,
                         mapa_despues=None, materializadas=None)

    ensure_sessions_table(cur)
    materializadas = refresh_account_sessions(cur, cuenta)

    cur.execute(_COUNT_SESIONES, (cuenta,))
    sesiones_despues = cur.fetchone()[0]
    cur.execute(_COUNT_MAPA, (cuenta,))
    mapa_despues = cur.fetchone()[0]
    return Resultado(cuenta=cuenta, sesiones_antes=sesiones_antes,
                     sesiones_despues=sesiones_despues, mapa_antes=mapa_antes,
                     mapa_despues=mapa_despues, materializadas=materializadas)


def formatear_reporte(resultados: list[Resultado], dry_run: bool = False) -> str:
    """Reporte legible con el DELTA a la vista (no solo los dos numeros)."""
    if not resultados:
        return "sin cuentas para procesar."
    lineas = []
    if dry_run:
        lineas.append("--dry-run: NO se escribio nada. Estado actual:")
        lineas.append(f"  {'cuenta':<12} {'sesiones':>12} {'mapa':>12}")
        for r in resultados:
            lineas.append(f"  {r.cuenta:<12} {r.sesiones_antes:>12,} {r.mapa_antes:>12,}")
        return "\n".join(lineas)

    lineas.append(f"  {'cuenta':<12} {'sesiones':>26} {'mapa (episodios)':>26}")
    for r in resultados:
        d_ses = r.sesiones_despues - r.sesiones_antes
        d_map = r.mapa_despues - r.mapa_antes
        lineas.append(
            f"  {r.cuenta:<12} "
            f"{r.sesiones_antes:>10,} -> {r.sesiones_despues:>9,} ({d_ses:+,})".ljust(52)
            + f"{r.mapa_antes:>9,} -> {r.mapa_despues:>9,} ({d_map:+,})"
        )
    lineas.append("")
    lineas.append("  Las sesiones huerfanas (session_id que dejo de ser inicio de sesion)")
    lineas.append("  las barre _ORPHAN_DELETE dentro del refresh: por eso el total puede BAJAR.")
    return "\n".join(lineas)
