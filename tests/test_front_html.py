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


# --- EL DESENLACE DEL CLIENTE: cuatro finales, no un booleano -----------------------
# El chip decia "↩ no contesto" para todo, y "no contestar" no informa nada por si solo: si
# el motivo se resolvio, es lo NATURAL. MEDIDO el 2026-08-12 sobre la copia de produccion,
# 252 sesiones: **39 clientes recibieron el pedido y NUNCA LO ABRIERON** (el balde mas
# grande, invisible hasta ahora), 23 lo leyeron y se fueron, 8 no lo recibieron y 1 dijo que
# no. Y 37 de los 39 estan en `promo` y `registro`: el embudo de prospeccion.
# Cada final se acciona distinto, asi que cada uno lleva su chip y su tip:
#   se_fue      fuga del embudo   ·  no_lo_abrio  lead frio
#   no_le_llego problema tecnico  ·  dijo_no      lead perdido

def _html():
    from pathlib import Path
    return Path("web/index.html").read_text(encoding="utf-8")


def test_el_front_distingue_los_cuatro_desenlaces():
    html = _html()
    for estado in ("se_fue", "no_lo_abrio", "no_le_llego", "dijo_no"):
        assert estado in html, f"falta el desenlace {estado!r}"


def test_cada_desenlace_tiene_su_TIP_propio():
    html = _html()
    # El tip es lo que explica QUE HACER con ese final; sin el, el chip es un adorno.
    i = html.find("const DESENLACE_TIP")   # la definicion, no el atributo del template
    assert i > 0, "falta el mapa de tips por desenlace"
    tramo = html[i:i + 1400]
    for estado in ("se_fue", "no_lo_abrio", "no_le_llego", "dijo_no"):
        assert estado in tramo, f"{estado} sin tip"


def test_el_chat_parte_por_interaccion_y_senala_la_calificada():
    # El modal mostraba la sesion entera como un chat corrido, y una sesion mergea TODOS los
    # episodios del ticket: hay de 41 interacciones. Quien auditaba leia una nota de 2★ al
    # lado de un tramo que habia salido bien. Se dibuja el corte Y se senala cual se califico.
    html = _html()
    assert "nuevaInteraccion(modal.detail.transcript, i)" in html, "el chat no dibuja el corte"
    assert "m.interaccion }} de {{ m.interacciones" in html, "el corte no dice cual de cuantas"
    assert "la calificada" in html, "no se senala la interaccion que la nota describe"
    # Las que NO se calificaron son contexto: se leen, no compiten con la que importa.
    assert ".bub.ajena" in html and "m.juzgada" in html


def test_la_espera_NO_se_mide_cruzando_una_frontera_de_interaccion():
    # El «⏳ sin respuesta» compara dos mensajes seguidos de distinto rol. Cruzando el corte
    # eso son el ultimo mensaje de una atencion CERRADA y el primero de la siguiente: un
    # salto de 51 h que se leia como una espera pendiente cuando el asunto ya estaba
    # terminado. Nadie estaba esperando nada: el operador habia cerrado.
    html = _html()
    i = html.index("function bubGap")
    cuerpo = html[i:i + 700]
    assert "interaccion" in cuerpo, "bubGap sigue midiendo la espera cruzando el corte"


def test_el_corte_no_aparece_cuando_hay_UNA_sola_interaccion():
    # El 96,3% de las sesiones son una sola. Ahi el separador es ruido y un "1 de 1" en cada
    # chat es peor que no decir nada: el guard va en la funcion, no en el template.
    html = _html()
    i = html.index("function nuevaInteraccion")
    cuerpo = html[i:i + 400]
    assert "< 2" in cuerpo or "<2" in cuerpo, "no corta cuando hay una sola interaccion"


def test_todo_lo_que_el_TEMPLATE_usa_esta_expuesto_en_el_setup():
    # Vue NO avisa: un nombre que el template usa y el `return` del setup no expone renderiza
    # CADENA VACIA. El chip de desenlace estuvo asi -- los dos mapas definidos, usados en el
    # template, y fuera del return: un chip vacio en cada conversacion con desenlace.
    # Se listan a mano los que el template consume de verdad; un barrido automatico se come la
    # prosa de los `title` y no distingue una variable de una palabra en español.
    html = _html()
    i = html.index("    return { accounts")
    ret = html[i:html.index("};", i)]
    for nombre in ("DESENLACE_CHIP", "DESENLACE_TIP", "SKIP_LABEL", "MOTIVO_LABEL",
                   "ATENCION_LABEL", "SEGMENT_LABEL", "motivoStats", "motivoCobertura",
                   "opName", "opTip", "nuevaInteraccion"):
        assert f" {nombre}," in ret or f" {nombre}\n" in ret, \
            f"{nombre} se usa en el template y el setup no lo expone: renderiza vacio"


def test_el_desenlace_no_se_lee_como_una_falla_del_operador():
    # Es un dato de CONTEXTO. El tip de cada uno tiene que decirlo, igual que hacia el de
    # abandono ("el tramite quedo abierto por el cliente, no por el operador").
    html = _html()
    i = html.find("const DESENLACE_TIP")   # la definicion, no el atributo del template
    tramo = html[i:i + 1400].lower()
    assert "no es" in tramo or "no cuenta" in tramo or "no depende" in tramo


# --- EL JS DEL TABLERO TIENE QUE PARSEAR ------------------------------------------
# El 2026-08-12 un bloque nuevo de constantes se inserto EN MEDIO del objeto `AYUDA`,
# partiendolo al medio y dejando su cola huerfana con un `};` suelto. Resultado: SyntaxError
# al cargar el <script>, el front NO MONTABA -- pantalla vacia, toda la app caida -- y la
# suite entera en verde. Los dos tests de balance de markup no lo ven porque el error esta en
# el JS, y el docstring de arriba cuenta el caso SIMETRICO: aquella vez el JS estaba bien y
# el error era del template. Hacen falta los dos chequeos, y este es el que faltaba.

def test_el_javascript_del_tablero_parsea():
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("node"):                     # pragma: no cover
        import pytest
        pytest.skip("node no esta disponible en este entorno")
    html = _html()
    bloques = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert bloques, "no se encontro ningun <script> inline: revisar este test"
    for n, cuerpo in enumerate(bloques):
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8",
                                         delete=False) as f:
            f.write(cuerpo)
            ruta = f.name
        r = subprocess.run(["node", "--check", ruta], capture_output=True, text=True)
        assert r.returncode == 0, f"el bloque <script> #{n} no parsea:\n{r.stderr}"


# --- EL MODAL NO PUEDE MOSTRAR CLAVES INTERNAS DE dimensions -------------------------
# `dimsOnly` era una DENY-LIST: pintaba CUALQUIER dimension de tipo string, con la clave
# cruda como titulo. O sea que cada clave nueva se filtraba sola. Aparecio en produccion el
# 2026-08-12 asi, en el modal, delante del negocio:
#     interaccion_juzgada_desde
#     2026-08-12T12:58:52.408000+00:00
# `cliente_desenlace` tenia el mismo destino (es string y tampoco estaba en la lista), ademas
# de su chip. Las notas en prosa que el modal SI debe mostrar son exactamente tres, y son las
# que declara el esquema del prompt (src/prompts.py): resolucion, iniciativa, cortesia.
# Invertido a ALLOW-LIST: una clave nueva sin etiqueta queda INVISIBLE, que es mucho mejor que
# filtrada. Es la misma leccion que la regla de identidad: una lista que hay que acordarse de
# actualizar a mano en cada agregado no es una regla, es una trampa.

def test_el_modal_solo_muestra_las_notas_en_prosa_conocidas():
    html = _html()
    i = html.index("const DIM_PROSA")
    bloque = html[i:html.index("\n", html.index("dimsOnly", i))]
    for prosa in ("resolucion", "iniciativa", "cortesia"):
        assert prosa in bloque, f"falta la nota en prosa {prosa!r}"
    for interna in ("interaccion_juzgada_desde", "cliente_desenlace", "recomendacion"):
        assert interna not in bloque, f"{interna} no es una nota en prosa para mostrar"
    # y que sea una ALLOW-list: la funcion tiene que consultar el mapa, no negarlo
    assert "_NON_DIM" not in bloque, "sigue siendo deny-list: cada clave nueva se filtra sola"
