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


# --- TODO skip_reason QUE EL CODIGO EMITE TIENE QUE TENER ETIQUETA EN EL FRONT -------
# El negocio reporto el 2026-08-13 que "la info de redireccion no se muestra en el dashboard".
# No era el scoring: `redireccion` dispara bien (7 de 9 candidatos del bucket C). Era que
# `SKIP_LABEL` no lo tenia, y el front cae a `SKIP_LABEL[x] || x` -> mostraba el string crudo
# `redireccion` en el snippet de la fila y en el modal, en vez de una frase legible.
#
# Es el mismo tipo de agujero que los dos de tests/test_coaching.py: la etiqueta se agrego a
# mano, nada ataba la lista del front a lo que el codigo REALMENTE puede escribir en la
# columna, y por eso un motivo nuevo (`redireccion`, del 2026-08-07) entro sin su etiqueta y
# nadie se entero hasta que alguien lo vio en pantalla.

def _skip_reasons_del_codigo() -> set[str]:
    """Los `skip_reason` que las fuentes pueden persistir, leidos del codigo."""
    encontrados: set[str] = set()
    for modulo in ("router.py", "sessions.py"):
        texto = (HTML.parents[1] / "src" / modulo).read_text(encoding="utf-8")
        # Se emite de DOS formas: `return "skipped", "<motivo>"` (router, 2 valores) y
        # `return stats, rubric, "skipped", "<motivo>"` (sessions, 4 valores). Anclar en
        # `return` solo veia la primera y el test pasaba en verde con el bug adentro --
        # justamente el agujero que este test viene a cerrar.
        encontrados |= set(re.findall(r'"skipped",\s*"(\w+)"', texto))
    return encontrados


def test_todo_skip_reason_del_codigo_tiene_etiqueta_en_el_front():
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const SKIP_LABEL = {")
    bloque = html[i:html.index("};", i)]
    etiquetados = set(re.findall(r'(\w+)\s*:\s*"', bloque))
    delcodigo = _skip_reasons_del_codigo()
    assert delcodigo, "no se pudo leer ningun skip_reason del codigo"
    faltan = delcodigo - etiquetados
    assert not faltan, (
        f"estos skip_reason se persisten y el front los muestra crudos: {sorted(faltan)}")


def test_las_etiquetas_de_skip_explican_y_no_repiten_la_clave():
    # Una etiqueta que repite la clave ("redireccion" -> "redireccion") no agrega nada: el
    # lector del tablero no sabe que significa. Tiene que ser una frase.
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const SKIP_LABEL = {")
    bloque = html[i:html.index("};", i)]
    for clave, texto in re.findall(r'(\w+)\s*:\s*"([^"]*)"', bloque):
        assert len(texto) >= 12, f"la etiqueta de {clave!r} es demasiado corta: {texto!r}"
        assert texto.lower() != clave.lower().replace("_", " "), \
            f"la etiqueta de {clave!r} repite la clave: {texto!r}"


# --- LA TARJETA DE "SIN EVALUAR POR CAUSA" ------------------------------------------
# Pedida por el negocio el 2026-08-13. El KPI decia CUANTAS quedaron sin evaluar y no habia
# forma de ver POR QUE: para contar las derivadas habia que filtrar la lista a ojo.

def test_la_tarjeta_de_sin_evaluar_por_causa_existe_y_usa_el_payload():
    html = HTML.read_text(encoding="utf-8")
    assert "Sin evaluar, por causa" in html, "falta el titulo de la tarjeta"
    assert "summary.skip_stats" in html, "la tarjeta no lee skip_stats del summary"
    assert "skipStats, skipTotal" in html, "skipStats/skipTotal no se exponen al template"


def _bloque_skip(html: str, largo: int = 2600) -> str:
    """El tramo del template que pinta las filas de 'sin evaluar'.

    Se ancla en el `v-for` y NO en el titulo: desde que las dos tarjetas se unificaron en
    una sola con switch (2026-08-14), el titulo vive en el <h2> y el contenido de esta vista
    queda despues de la vista de calidad, fuera de cualquier ventana razonable.
    """
    return html[html.index('v-for="s in skipStats"'):][:largo]


def test_la_tarjeta_de_skip_traduce_la_causa_en_vez_de_mostrar_la_clave():
    # El bug original era exactamente este: mostrar `redireccion` crudo.
    html = HTML.read_text(encoding="utf-8")
    assert "SKIP_LABEL[s.skip_reason]" in _bloque_skip(html), "la tarjeta no pasa por SKIP_LABEL"


def test_la_tarjeta_de_skip_SI_es_clicable():
    """DADO VUELTA el 2026-08-14. Antes este test exigia lo contrario, y la razon era
    correcta para su momento: "el filtro de estado es Todas/Evaluadas/Sin evaluar y no sabe
    de causas, asi que un clic mostraria TODO lo salteado y no la fila apretada".

    Ahora el filtro SI sabe de causas (`causa` -> `cs.skip_reason`, ver
    tests/test_queries.py::test_scores_filters_filtra_por_CAUSA_de_sin_evaluar), asi que el
    clic muestra exactamente la fila apretada y la promesa se cumple.
    """
    html = HTML.read_text(encoding="utf-8")
    bloque = _bloque_skip(html)
    assert "toggleCausa(s.skip_reason)" in bloque, "la fila tiene que filtrar por su causa"
    assert 'role="button"' in bloque, "tiene que anunciarse como boton"
    assert "distrow static" not in bloque, "ya no son filas estaticas"


def test_el_filtro_de_causa_viaja_al_backend():
    html = HTML.read_text(encoding="utf-8")
    assert 'p.set("causa", filters.causa)' in html, "la causa no se manda en el query string"
    assert 'causa:"all"' in html, "la causa no esta en FILTER_DEFAULTS (se arrastraria entre cuentas)"
    assert "causa:   { label:" in html, "sin entrada en FILTER_META no aparece como chip activo"


def test_el_encabezado_con_switch_no_lleva_la_descripcion_adentro():
    """La descripcion NO puede vivir en el <h2> cuando la tarjeta tiene un control.

    Los `small` de este tablero son frases largas con varios `·`, y en un flex con
    `flex-wrap` empujaban el switch o se le metian encima al envolver. El titulo queda
    corto y solo, el control fijo a la derecha (`.ctl` con margin-left:auto) y la
    descripcion baja a `.card-sub`, su propia linea.
    """
    html = HTML.read_text(encoding="utf-8")
    i = html.index('<h2 class="con-ctl">')
    encabezado = html[i:html.index("</h2>", i)]
    assert "<small>" not in encabezado, "la descripcion volvio al titulo y va a chocar con el switch"
    assert 'class="segmented ctl"' in encabezado, "el control tiene que anclarse a la derecha"
    # Y la descripcion existe, en su propia linea, para las dos vistas.
    assert html.count('class="card-sub"') >= 2


def test_las_dos_vistas_viven_en_UNA_tarjeta_con_switch():
    """Eran dos tarjetas con el MISMO layout en una grilla de tres columnas, asi que la
    segunda caia sola en una fila nueva: no era una decision, era el sobrante."""
    html = HTML.read_text(encoding="utf-8")
    assert html.count('class="trio"') == 1
    inicio = html.index('class="trio"')
    trio = html[inicio:html.index('<div class="cols">', inicio)]
    assert trio.count('<div class="card">') == 3, "la grilla de 3 columnas tiene que tener 3 cards"
    assert 'vistaMotivo===\'calidad\'' in trio, "falta la vista de calidad"
    assert 'vistaMotivo=\'sin_evaluar\'' in trio, "falta el boton de la vista de sin evaluar"


def test_la_alerta_de_jugador_sin_respuesta_solo_mira_UNA_situacion():
    # La INTENCION del test se conserva entera: la alerta se cuelga de UNA sola causa y no de
    # todas. En `datos` casi todo es del segmento jugador (429 de 431 en `sin_motivo`, 14 de 15
    # en `solo_cortesia`), asi que marcarlo en cada renglon seria ruido. La alerta significa
    # algo distinto: un jugador escribio y NADIE contesto.
    # LO QUE CAMBIO el 2026-08-24 es DE DONDE sale. `no_agent_reply` dejo de ser un skip el
    # 2026-08-21 y paso a llevar 1 estrella, asi que el codigo lo emite CERO veces y la alerta
    # quedaba en 0 para siempre. Ahora sale de `situacion_stats`, sobre filas evaluadas.
    # (Y los 160 grupos que vivian en ese mismo renglon ya no llegan: se saltean antes con
    # `grupo_de_whatsapp`, ver src/router.py.)
    html = HTML.read_text(encoding="utf-8")
    assert "jugadorSinRespuesta" in html
    m = re.search(r"const jugadorSinRespuesta = computed\((.*?)\);", html, re.S)
    assert m, "cambio la forma de jugadorSinRespuesta, revisar este test"
    assert "'sin_respuesta_del_negocio'" in m.group(1), \
        "la alerta tiene que colgarse SOLO de sin_respuesta_del_negocio"
    assert "'solo_cortesia'" not in m.group(1), \
        "`solo_cortesia` es casi todo jugador: marcarlo ahi es ruido, no alerta"


def test_la_alerta_de_jugador_se_pinta_como_problema():
    html = HTML.read_text(encoding="utf-8")
    bloque = _bloque_skip(html, 3500)
    assert "v-if=\"jugadorSinRespuesta\"" in bloque, "la alerta no esta en la tarjeta"
    assert "var(--r-malo)" in bloque, "la alerta tiene que leerse como un problema, no como un dato"


def test_el_chat_muestra_LA_CAUSA_del_skip_y_no_un_generico():
    """Toda sesion salteada mostraba el mismo chip "sin evaluar" en la lista de chats.

    El negocio lo reporto dos veces con `redireccion`: la causa vivia SOLO en el cuadro
    agregado, asi que abrir el chat no decia por que habia quedado afuera -- y una sesion
    derivada a otra linea nuestra se leia igual que una donde el cliente no planteo nada.
    El dato ya viajaba en la fila (`skip_reason`); solo faltaba pintarlo.
    """
    html = HTML.read_text(encoding="utf-8")
    i = html.index('class="chip skip"')
    chip = html[i:i + 260]
    assert "SKIP_LABEL[cv.skip_reason]" in chip, "el chip del chat sigue siendo generico"


def test_TODO_filtro_de_FILTER_DEFAULTS_dispara_una_recarga():
    """El watcher del debounce es una lista EXPLICITA de `filters.x`, igual que el dict de
    `_filters` en el backend: se agrega un filtro nuevo, el chip aparece, el estado cambia
    y NO SE RECARGA NADA. Paso exactamente eso con `causa` el 2026-08-14 -- el cuadro de
    calidad filtraba y el de sin evaluar no, con el mismo codigo de toggle.

    Este test recorre FILTER_DEFAULTS y exige que cada clave se observe en algun `watch`,
    asi que cubre tambien el proximo filtro que se agregue.
    """
    html = HTML.read_text(encoding="utf-8")
    defaults = html[html.index("const FILTER_DEFAULTS = {"):]
    defaults = defaults[:defaults.index("};")]
    # Sin los comentarios: "// Baja lógica de operadores:" aportaba una clave inexistente.
    defaults = re.sub(r"//[^\n]*", "", defaults)
    claves = set(re.findall(r"(\w+)\s*:", defaults))
    # `sort` es ORDEN, no filtro: tiene su propio watcher y no cambia la poblacion.
    # `ambiente` es un cambio de CONTEXTO: recarga desde `setAmbiente`, no por watcher.
    # `inactivos` tiene watcher propio porque ademas mueve /api/charts.
    exentos = {"sort", "ambiente", "inactivos"}
    # EL WATCHER DEL DEBOUNCE, no cualquiera. La primera version de este test pedia que la
    # clave apareciera en ALGUN `watch` y pasaba con el bug adentro: `causa` estaba observada
    # por el watcher que cambia la pestaña, que no recarga nada.
    m = re.search(r"watch\(\(\) => \[([^\]]*)\], debouncedFilter\)", html)
    assert m, "cambio la forma del watcher del debounce, revisar este test"
    observados = m.group(1)
    for clave in claves - exentos:
        assert f"filters.{clave}" in observados, \
            f"'{clave}' esta en FILTER_DEFAULTS pero no dispara la recarga: el chip cambia y no pasa nada"


# --- EL FRONT TIENE QUE CONOCER TODO EL VOCABULARIO DEL BACKEND -------------------------
def test_todo_motivo_del_codigo_tiene_etiqueta_en_el_front():
    """`redireccion` entro como motivo el 2026-08-20 y NADA obligaba al front a nombrarlo:
    habria salido crudo en el filtro y en la tarjeta de calidad. Espeja
    `test_todo_skip_reason_del_codigo_tiene_etiqueta_en_el_front`, que si existia.

    SE PARSEA EL OBJETO, no se busca el string en el HTML. La primera version buscaba
    `"<motivo>:"` en todo el archivo y PASABA CON EL BUG PUESTO -- la palabra aparecia en
    otro lado. Un test que no falla con el defecto presente no protege nada.
    """
    from src.rubrics import MOTIVOS

    html = _html()
    i = html.index("const MOTIVO_LABEL = {")
    cuerpo = html[i:html.index("};", i)]
    faltan = [m for m in MOTIVOS if f"{m}:" not in cuerpo]
    assert not faltan, f"motivos sin etiqueta en MOTIVO_LABEL: {faltan}"


def test_el_front_muestra_la_practica_del_manual_del_coaching():
    """El enganche con B01-B12 es lo que vuelve contable el coaching en el idioma de ATC.

    SE EXIGE LA DEFINICION, no la mencion. La primera version pedia `"dimPractica" in html`
    y PASABA aunque la funcion no existiera, porque el TEMPLATE la nombra igual -- y en Vue
    eso no es un error, renderiza vacio. Hay que verificar que este DEFINIDA y expuesta.
    """
    html = _html()
    assert "function dimPractica(" in html, "el template la llama y no esta definida"
    i = html.index("    return { accounts")
    ret = html[i:html.index("};", i)]
    assert " dimPractica," in ret, "definida pero fuera del return: renderiza vacio"
    # y sale del catalogo pedido a /api/catalogo, no hardcodeado
    assert "catalogo.practicas[cod]" in html


def test_el_front_explica_las_situaciones_que_antes_eran_un_skip():
    """`no_agent_reply` y `sin_motivo` dejaron de saltearse y ahora llevan nota. El
    `skip_reason` ya no esta en la fila -- el CHECK de la tabla lo borra en las evaluadas --,
    asi que sin este bloque un supervisor ve una nota sin causa."""
    html = _html()
    assert "function dimSituacion(" in html, "el template la llama y no esta definida"
    i = html.index("    return { accounts")
    assert " dimSituacion," in html[i:html.index("};", i)], "fuera del return: renderiza vacio"
    assert "sin_respuesta_del_negocio" in html
    assert "solo_cortesia" in html
    assert "destino_utilizable" in html


# --- LA TARJETA QUE SE MURIO CUANDO EL SKIP SE VOLVIO NOTA -----------------------------
# `jugadorSinRespuesta` buscaba `skip_reason === 'no_agent_reply'`, y desde el 2026-08-21 el
# codigo emite ese skip CERO veces: la alerta "N de canal jugador sin respuesta del negocio"
# no se podia mostrar nunca mas. El chip POR FILA si se habia migrado (`dimSituacion`), pero
# el agregado no -- y el agregado era lo que el negocio habia pedido el 2026-08-13.

def test_el_front_lee_las_situaciones_del_summary():
    html = HTML.read_text(encoding="utf-8")
    assert "situacion_stats" in html, (
        "el agregado viaja en /api/summary y el front no lo lee: la alerta sigue muerta")


def test_la_alerta_del_jugador_NO_se_cuelga_de_un_skip_que_ya_no_existe():
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const jugadorSinRespuesta")
    bloque = html[i:i + 400]
    assert "no_agent_reply" not in bloque, (
        "sigue buscando un skip_reason que el codigo emite 0 veces -> siempre 0")
    assert "sin_respuesta_del_negocio" in bloque, (
        "la situacion vive en dimensions desde el 2026-08-21")


def test_las_situaciones_tienen_etiqueta_en_el_front():
    """Mismo agujero que `SKIP_LABEL` en su momento: sin etiqueta el tablero muestra la
    clave cruda (`sin_respuesta_del_negocio`) al que mira."""
    html = HTML.read_text(encoding="utf-8")
    i = html.index("const SITUACION_LABEL = {")
    bloque = html[i:html.index("};", i)]
    for clave in ("sin_respuesta_del_negocio", "solo_cortesia"):
        assert clave in bloque, f"falta la etiqueta de {clave}"
    for clave, texto in re.findall(r'(\w+)\s*:\s*"([^"]*)"', bloque):
        assert len(texto) >= 12, f"la etiqueta de {clave!r} es demasiado corta: {texto!r}"
        assert texto.lower() != clave.lower().replace("_", " "), \
            f"la etiqueta de {clave!r} repite la clave"


def test_las_situaciones_del_codigo_TIENEN_etiqueta_en_el_front():
    """Ata la lista del front a lo que la consulta REALMENTE puede devolver, en vez de a una
    lista escrita a mano. Es el mismo contrato que
    `test_todo_skip_reason_del_codigo_tiene_etiqueta_en_el_front`."""
    from src.queries import _SITUACION_FLAGS

    html = HTML.read_text(encoding="utf-8")
    i = html.index("const SITUACION_LABEL = {")
    bloque = html[i:html.index("};", i)]
    faltan = [f for f in _SITUACION_FLAGS if f not in bloque]
    assert not faltan, f"estas situaciones se muestran crudas: {faltan}"


def test_las_situaciones_se_LISTAN_con_su_numero_y_no_solo_la_alerta():
    """La alerta dice cuantas son de jugador; sin la lista, el fenomeno completo sigue sin
    numero. Es lo que se perdio el 2026-08-21."""
    html = HTML.read_text(encoding="utf-8")
    i = html.index('v-if="situacionStats.length"')
    bloque = html[i:i + 900]
    assert "SITUACION_LABEL[s.situacion]" in bloque, "muestra la clave cruda"
    assert "fmtN(s.n)" in bloque, "no muestra el conteo"
    assert "s.estrellas" in bloque, "no muestra el promedio de estrellas"


def test_las_situaciones_NO_se_suman_al_total_de_sin_evaluar():
    """Estas filas SI se evaluan: meterlas en `skipTotal` rompe el cierre contra el KPI, que
    es lo unico que avisa si el desglose se desincroniza."""
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r"const skipTotal = computed\((.*?)\);", html, re.S)
    assert m, "cambio la forma de skipTotal"
    assert "situacion" not in m.group(1)


# --- /api/ambientes: el endpoint que existia y nadie llamaba ---------------------------
# Su docstring en src/app.py dice literal: "Es la respuesta a 'no se sabe de que son que': el
# front puede decir que compone el numero que esta mostrando, en vez de que el usuario lo
# deduzca". Estaba escrito, probado y SIN CABLEAR -- el front no lo llamaba ni una vez.
# Auditoria del 2026-08-24, pedida por el usuario ("siento que hay info faltante").

def test_el_front_llama_a_api_ambientes():
    html = HTML.read_text(encoding="utf-8")
    assert "/api/ambientes" in html, "el endpoint sigue sin cablear"


def test_la_composicion_se_pide_UNA_vez_por_cuenta_no_por_filtro():
    """Es estable por cuenta (el agregado recorre las conversaciones), asi que pedirla en
    cada cambio de filtro seria gasto puro. Mismo criterio que /api/options."""
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r"/api/ambientes\?[^\"']*", html)
    assert m, "no se encontro la llamada"
    assert "ambiente=" not in m.group(0), (
        "la composicion NO se filtra por ambiente: devuelve los cuatro de una")


def test_la_composicion_se_MUESTRA_con_la_cola_y_su_volumen():
    html = HTML.read_text(encoding="utf-8")
    i = html.index('v-if="composicionActual.length"')
    bloque = html[i:i + 700]
    assert "c.cola" in bloque, "no nombra la cola"
    assert "c.conversaciones" in bloque, "no muestra el volumen de la cola"
