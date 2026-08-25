"""La etiqueta de la vara no puede mentir.

`SCORING_VERSION` es lo unico que dice CON QUE VARA se calculo una fila, y el propio
store.py declara que "el bump es OBLIGATORIO cada vez que cambia como se califica".
Nada lo ataba al changelog, y por eso quedo en `2026.08-rubricas-v21` mientras los
commits del 2026-08-21 y del 2026-08-24 movian notas: catorce correcciones y 3.072
filas de la copia etiquetadas con una vara que ya no era la suya.

NO ROMPE EL RESCORE (`scripts/snapshot_scores.py` filtra por `scored_at`), pero un
supervisor que abre una fila y lee la version se lleva un dato falso, y un rescore
parcial no se puede reconstruir despues.
"""
import re
from pathlib import Path

from src.store import SCORING_VERSION

STORE = Path(__file__).parents[1] / "src" / "store.py"
# Los encabezados del changelog: `# 2026.08-rubricas-v21 (2026-08-19). ...`
_ENTRADA = re.compile(r"^# (\d{4}\.\d{2}-[\w.\-]+) \(\d{4}-\d{2}-\d{2}\)\.", re.MULTILINE)


def _entradas() -> list[str]:
    return _ENTRADA.findall(STORE.read_text(encoding="utf-8"))


def test_la_version_tiene_su_entrada_en_el_changelog():
    entradas = _entradas()
    assert entradas, "no se pudo leer ninguna entrada del changelog de store.py"
    assert SCORING_VERSION in entradas, (
        f"{SCORING_VERSION!r} no tiene entrada en el changelog. Las ultimas son: "
        f"{entradas[-3:]}")


def test_la_version_es_la_ULTIMA_entrada_del_changelog():
    """El bump y la entrada van juntos: si alguien documenta una vara nueva y se olvida
    de mover la constante, las filas siguientes salen etiquetadas con la vieja."""
    entradas = _entradas()
    assert SCORING_VERSION == entradas[-1], (
        f"la constante dice {SCORING_VERSION!r} pero la ultima entrada del changelog es "
        f"{entradas[-1]!r}: una de las dos quedo atras")
