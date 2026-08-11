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
import re
from html.parser import HTMLParser

# Elementos sin cierre (HTML5). No van a la pila.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# Dentro de estos el contenido no es markup (llaves de JS, CSS, etc.).
CRUDO = {"script", "style"}

HTML = pathlib.Path(__file__).resolve().parents[1] / "web" / "index.html"


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


# --- EL RECORTE DE LAS TARJETAS POR OPERADOR --------------------------------------
# Son dos decisiones del negocio (2026-08-11) que viven en dos constantes de JS, o sea que
# nada las protege salvo esto. Un `expand` en true o un OPS_CAP en 8 no rompen ningun test
# ni tiran ningun error: simplemente el tablero vuelve a verse mal.

def test_las_tarjetas_por_operador_arrancan_PLEGADAS():
    # Con 50 operadores, arrancar expandido convierte la tarjeta en un muro. El default es
    # el recorte; ver todos queda a un click.
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r"const expand = reactive\(\{([^}]*)\}\)", html)
    assert m, "cambio la forma de `expand`, revisar este test"
    for clave in ("qual", "convPasv", "qualMotivo"):
        assert re.search(rf"\b{clave}\s*:\s*false", m.group(1)), \
            f"expand.{clave} tiene que arrancar en false: {m.group(1).strip()}"


def test_el_recorte_completa_hileras_de_6():
    # La grilla es `auto-fill` con minmax(235px), asi que las columnas dependen del ancho: en
    # pantalla grande entran 6. Con 8 la segunda hilera quedaba con 2 tarjetas y 4 huecos.
    # El cap tiene que dividir a 6 (y de paso a 4, 3 y 2) para no dejar huecos.
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r"const OPS_CAP = (\d+)", html)
    assert m, "cambio el nombre de OPS_CAP, revisar este test"
    cap = int(m.group(1))
    for columnas in (2, 3, 4, 6):
        assert cap % columnas == 0, f"OPS_CAP={cap} deja huecos con {columnas} columnas"


# --- LOS CHIPS DE HECHOS TIENEN QUE LEERSE EN LAS DOS DIRECCIONES -----------------
# El mismo hecho se pinta con ✓ cuando se cumple y con ✗ cuando no. Con UNA sola frase
# escrita como logro, el ✗ queda como acertijo: "✗ le mandó algo concreto" no dice ni qué es
# ni qué habia que hacer. Lo trajo el negocio el 2026-08-11: "yo lo sé, vos lo sabés, pero
# alguien que use este dashboard no tiene idea".

def test_cada_hecho_tiene_las_dos_caras_y_un_tip():
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const HECHO_LABEL = {")
    bloque = html[i:html.index("\n};", i)]
    claves = re.findall(r"^  (\w+): \{", bloque, re.M)
    assert len(claves) >= 7, f"se encontraron pocos hechos: {claves}"
    for clave in claves:
        entrada = re.search(rf"  {clave}: \{{(.*?)\}},", bloque, re.S).group(1)
        for cara in ("si", "no", "tip"):
            assert re.search(rf'\b{cara}: "[^"]{{10,}}"|\b{cara}: \'[^\']{{10,}}\'', entrada), \
                f"el hecho {clave!r} no tiene un {cara!r} con texto"


def test_la_cara_negativa_no_es_una_negacion_del_logro():
    # La regla: `no` describe LO QUE PASÓ. Un "no hizo X" reintroduce el acertijo, porque
    # obliga al lector a saber qué era X.
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const HECHO_LABEL = {")
    bloque = html[i:html.index("\n};", i)]
    for clave, texto in re.findall(r"^  (\w+): \{[^}]*?\bno: \"([^\"]+)\"", bloque, re.M | re.S):
        assert not texto.lower().startswith(("no ", "no le", "sin ")), \
            f"el hecho {clave!r} arranca negando en vez de contar: {texto!r}"
