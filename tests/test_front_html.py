"""El HTML del tablero tiene que estar BALANCEADO.

POR QUE EXISTE ESTE TEST. El 2026-08-07 agregue un boton de ayuda en el modal y lo deje
FUERA de la fila de chips, dejando un `</div>` de mas. Ese cierre extra cerraba el
`v-else-if="modal.detail"` antes de tiempo, asi que `{{ modal.detail.rating_rationale }}`
quedaba fuera del guard y se renderizaba con `detail` en null:

    TypeError: Cannot read properties of null (reading 'rating_rationale')

El front NO MONTABA — pantalla vacia, toda la app caida. Y `node --check` sobre el bloque
de <script> pasaba perfecto, porque el JS estaba bien: el error estaba en el TEMPLATE.
La suite entera en verde y el tablero roto.

`web/index.html` es un archivo de ~1.700 lineas que se edita a mano, y un cierre de mas no
da ningun sintoma hasta que el navegador lo monta. Este test lo convierte en un fallo.
"""
import pathlib
from html.parser import HTMLParser

# Elementos sin cierre (HTML5). No van a la pila.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# Dentro de estos el contenido no es markup (llaves de JS, CSS, etc.).
CRUDO = {"script", "style"}


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila: list[tuple[str, int]] = []
        self.errores: list[str] = []
        self._crudo: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._crudo:
            return
        if tag in CRUDO:
            self._crudo = tag
            return
        if tag in VOID:
            return
        self.pila.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        pass  # <tag /> abre y cierra

    def handle_endtag(self, tag):
        if self._crudo:
            if tag == self._crudo:
                self._crudo = None
            return
        if tag in VOID:
            return
        if not self.pila:
            self.errores.append(f"linea {self.getpos()[0]}: </{tag}> sin apertura")
            return
        abierto, linea = self.pila[-1]
        if abierto == tag:
            self.pila.pop()
            return
        # cierre cruzado: buscar si el tag existe mas atras en la pila
        for i in range(len(self.pila) - 1, -1, -1):
            if self.pila[i][0] == tag:
                sobrantes = [f"<{t}> (linea {l})" for t, l in self.pila[i + 1:]]
                self.errores.append(
                    f"linea {self.getpos()[0]}: </{tag}> cierra pero quedan abiertos "
                    + ", ".join(sobrantes))
                del self.pila[i:]
                return
        self.errores.append(
            f"linea {self.getpos()[0]}: </{tag}> sin apertura "
            f"(el ultimo abierto es <{abierto}> de la linea {linea})")


def _analizar():
    ruta = pathlib.Path(__file__).resolve().parent.parent / "web" / "index.html"
    p = _Balance()
    p.feed(ruta.read_text(encoding="utf-8"))
    return p


def test_el_html_del_tablero_esta_balanceado():
    p = _analizar()
    assert not p.errores, "markup desbalanceado en web/index.html:\n  " + "\n  ".join(p.errores)


def test_no_quedan_etiquetas_sin_cerrar():
    p = _analizar()
    colgando = [f"<{t}> abierto en la linea {l}" for t, l in p.pila]
    assert not colgando, "etiquetas sin cerrar:\n  " + "\n  ".join(colgando)
