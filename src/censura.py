"""Enmascarar dato sensible EN LA SALIDA del tablero.

EL PROBLEMA. El tablero se sirve en un dominio publico y tiene 14 endpoints de lectura
ANONIMOS que devuelven `customer_number` y transcripts completos (auditoria del
2026-08-24). MEDIDO sobre los 52.135 mensajes de las sesiones scoreadas:

    celular EC                    3.734 (7,16%)   cliente 533 · operador 3.201
    10 digitos (cedula o cuenta)  2.186 (4,19%)   cliente 1.288 · operador 898
    monto de dinero               1.696 (3,25%)   -- NO se censura, ver abajo
    cuenta bancaria                 685 (1,31%)
    cedula declarada                612 (1,17%)
    CREDENCIALES EN CLARO           399 (0,77%)   operador 384
    correo                          354 (0,68%)   cliente 353

LO MAS GRAVE NO SON LOS NUMEROS: son las 399 credenciales en claro, 384 escritas por
operadores. Un telefono expuesto molesta; un usuario y una clave dejan entrar a la
cuenta de una persona.

DONDE VA, Y ES LA DECISION QUE DEFINE EL MODULO: en la SALIDA. Nunca en la base, nunca
antes de calificar. Las señales deterministas viven de estos mismos digitos:
`redireccion.es_traspaso` compara el tail del telefono contra el mapa de lineas propias,
`registro._es_traspaso_de_datos` busca el email o la cedula, `signals.operator_sent_credentials`
busca justamente el patron de credenciales, y `registro.datos_completos_del_alta` exige
correo Y digitos. Censurar antes de calificar no protege a nadie y rompe media docena de
rubricas. Este modulo es PURO y no lo importa ningun modulo de scoring: si algun dia
aparece un import de `censura` en `src/scorer.py` o en una rubrica, es un bug.

LOS MONTOS NO SE CENSURAN A PROPOSITO. "$5" no identifica a nadie y el supervisor lo
necesita para juzgar un deposito o una recarga minima. Un dato que no expone y que hace
falta no se tapa: censurar de mas vuelve el tablero inutil y empuja a la gente a pedir
acceso a la base, que es peor.

EL NOMBRE Y EL APELLIDO SI SE TAPAN, desde el 2026-08-26. La premisa que los dejaba
afuera --"detectar un nombre en texto libre necesita una lista"-- se cayo al medir la
copia: casi todo lo que expone viene con ETIQUETA, en el formulario de retiro, que es la
MISMA forma que las credenciales ya resuelven. No hay que adivinar que palabra es un
nombre; alcanza con leer el campo que el propio formulario declara.

    Monto a retirar: 15
    Nombre completo del titular de la cuenta: Domenica Emperatriz Vera Olaya
    Cedula o DNI: 120856743-6

MEDIDO sobre los 69.394 mensajes que el tablero puede servir: 1.368 tocados (1,97%), CERO
no idempotentes, y de los 590 formularios que venian llenos se tapan 582 (98,6%).

`contacts.name` NO SE USA, y no por costo: el negocio senalo que ahi casi nunca hay un
nombre de persona (son usuarios de Facebook e Instagram, publicos de por si). Hay un
motivo mas fuerte: el TITULAR de la cuenta bancaria suele ser OTRA persona --la madre, el
hermano--, asi que la columna no contiene el dato que se filtra.

EL NOMBRE DEL OPERADOR SIGUE EN CLARO y no es un descuido: es el EJE del tablero, sin el
no hay a quien coachear. Viaja en el cuerpo como `*Miguel:*` --una etiqueta con dos puntos,
identica en FORMA a las del formulario--, y por eso el patron se ancla en la PALABRA de la
etiqueta y nunca en la forma.
"""
from __future__ import annotations

import re

# El largo minimo para mostrar los extremos. Con 4 caracteres, ver el primero y el
# ultimo revela la mitad del dato; con 6 ya no dice nada util a quien lo quiera usar.
_MIN_EXTREMOS = 6


def enmascarar(token: str | None) -> str:
    """`812345671` -> `8*******1`. Conserva el LARGO, que es la forma que pidio el negocio.

    El largo se conserva porque es informacion util y NO identifica: distingue una cedula
    de diez digitos de una cuenta de ocho, y deja ver que el dato estaba ahi en vez de
    borrarlo en silencio.
    """
    t = token or ""
    if len(t) < _MIN_EXTREMOS:
        return "*" * len(t)
    return t[0] + "*" * (len(t) - 2) + t[-1]


# --- que se censura ---------------------------------------------------------
#
# El ORDEN IMPORTA: las credenciales van primero porque su patron incluye el valor que
# los otros patrones tambien matchearian (un usuario tipo `Paula2026` tiene digitos), y
# el correo va antes que los numeros porque un email puede llevar una corrida de digitos
# que se comeria el patron de cedula, dejando el resto del correo visible.

# CREDENCIALES: se captura el VALOR y se deja la etiqueta, para que el lector sepa que
# hubo una entrega -- que es justo lo que la rubrica de `registro` premia -- sin poder usarla.
#
# PEDIR NO ES ENTREGAR, y esa distincion YA la resolvio el repo. La primera version de este
# modulo tapaba lo que seguia a "usuario" SIEMPRE, y sobre los mensajes reales se comia
# palabras comunes: "me ayudas con tu usuario porfa" -> tapaba "porfa"; "Su contraseña es
# tal cual su usuario" -> tapaba "tal" y dejaba la frase incomprensible. Las formas de abajo
# ESPEJAN `signals.CREDENTIALS_PATTERN`, que exige el orden `su X es` / `usuario: X`
# justamente para eso (ver su comentario: "me ayuda con su usuario", "cual es su clave" e
# "indiqueme su usuario" no matchean).
#
# EL VALOR TIENE QUE PARECER UNA CREDENCIAL: cuatro caracteres o mas y al menos un
# digito. "Su contraseña es tal cual su usuario" pasa la FORMA y no entrega nada; el
# digito deja fuera la palabra castellana suelta sin perder ningun `Paula2026` ni
# `Sorti2026.` del corpus, y el largo minimo evita comerse un monto ("la clave es 5").
# NO SE USA `[A-Z][a-z]` PARA EXIGIR CamelCase: con `re.IGNORECASE` esa clase matchea
# cualquier par de letras y "tal" volvia a pasar. El flag anula la intencion del patron.
#
# EL ARTICULO ES MAS AMPLIO QUE EN `signals.CREDENTIALS_PATTERN` (que exige `tu|su`) y es
# deliberado: alla un falso positivo INFLA una nota, aca solo tapa un dato de mas. Es la
# misma leccion que `signals._frases`, calibrada para suprimir y venenosa para habilitar --
# la polaridad del riesgo decide cuanto se puede aflojar un patron, y aca esta invertida.
_ETIQUETA = r"usuario|user|contrase[nñ]a|contrasena|clave|password|pass"
_VALOR = r"(?=[^\s,;]*\d)[^\s,;]{4,}"
_CRED_DOS_PUNTOS = re.compile(rf"((?:{_ETIQUETA})\s*[:=][ \t]*)({_VALOR})", re.IGNORECASE)
_CRED_ES = re.compile(
    rf"((?:tu|su|la|el|mi)\s+(?:{_ETIQUETA})\s+(?:es|sera|será)\s*:?\s+)({_VALOR})",
    re.IGNORECASE)

# CORREO. Se conserva el arroba y el dominio: sin el arroba el lector no sabe que ahi
# habia un correo, y el dominio no identifica a nadie (gmail, hotmail).
#
# EL `*` ENTRA EN LA CLASE DE LA PARTE LOCAL, y no es un detalle: sin el, la segunda
# pasada capturaba solo los caracteres de palabra pegados al arroba -- la `z` final de
# `e****...z` -- y se los comia. MEDIDO sobre los 25.282 mensajes distintos de la copia:
# **345 no eran idempotentes**, todos por esto, y el test unitario original no lo vio
# porque probaba con un telefono (donde el asterisco no puede volver a matchear).
# Con el `*` adentro se captura el token YA enmascarado completo, y `enmascarar` sobre
# `e********z` devuelve `e********z`: estable por construccion, no por casualidad.
_CORREO = re.compile(r"([\w.+\-*]+)(@[\w-]+\.\w{2,})")

# TELEFONO ecuatoriano, con o sin prefijo de pais.
_TELEFONO = re.compile(r"\+?593\d{9}\b|\b0\d{9}\b")

# CUALQUIER corrida larga de digitos: cedula (10), cuenta bancaria (8-12), RUC (13). Va
# DESPUES del telefono para no partir un numero con prefijo.
_DIGITOS = re.compile(r"\b\d{6,}\b")

# EL MONTO NO SE TOCA. Se lo reconoce solo para poder EXCLUIRLO del patron de digitos:
# "$5" y "20,00" tienen menos de 6 digitos, asi que `_DIGITOS` no los alcanza y no hace
# falta ningun guard. Se deja el patron documentado para que nadie lo agregue por error.
_MONTO_NO_SE_CENSURA = re.compile(r"\$\s?\d+([,.]\d{2})?")


# --- EL NOMBRE Y EL APELLIDO ------------------------------------------------
#
# DOS REGLAS, y la segunda solo existe porque la primera no llega a todo.
#
# 1. LA ETIQUETA. Misma forma que las credenciales: etiqueta, separador, valor. Cubre el
#    formulario de retiro entero (`Titular de la cuenta:`, `Nombres:` / `Apellidos:`,
#    `Nombre completo:`) y la plantilla de banco que manda el operador (`*Nombre:* X`).
# 2. LA LINEA SUELTA. El nombre solo, en su propia linea, sin ninguna etiqueta, dentro de
#    un mensaje que ya trae la cedula y la cuenta. Es un heuristico y por eso vive acotado.
#
# EL PISO DE DOS PALABRAS ES DE LA REGLA 2, NO DE LA 1, y lo dicto el negocio: "si es solo
# un nombre no se puede sacar mucha info, ahora si es un nombre y un apellido puede ser un
# problema". Detras de una etiqueta una palabra sola SI se tapa, porque el formulario ya
# declaro que es un apellido y la otra mitad esta en el campo de al lado.

_LETRA = r"A-Za-zÁÉÍÓÚÜÑáéíóúüñ"

_ET_NOMBRE = (r"nombres?\s+completos?(?:\s+del?\s+titular(?:\s+de\s+(?:la\s+)?cuenta)?)?"
              r"|nombre\s+(?:del?\s+)?titular(?:\s+de\s+(?:la\s+)?cuenta)?"
              r"|titular(?:\s+de\s+(?:la\s+)?cuenta)?"
              r"|nombres?|apellidos?")

# LA AGENCIA ES UN NOMBRE COMERCIAL Y EL USUARIO ES LA CREDENCIAL. Ninguno es una persona,
# y el segundo importa doble: `registro` premia que se vea la ENTREGA del usuario, asi que
# taparlo con el patron de nombres esconderia justo lo que el supervisor tiene que juzgar.
_NO_PERSONA = r"(?!\s*(?:de\s+|del\s+|de\s+la\s+)?(?:agencia|usuario|user|perfil|banco))"

# LAS OTRAS ETIQUETAS DEL MISMO FORMULARIO. El valor no puede EMPEZAR con una: cuando el
# campo viene vacio, lo que sigue a `Nombres:` es la etiqueta de abajo. Sin esto, la
# plantilla de registro (`Nombres:` / `Correo electronico:`) tapaba la palabra "Correo"
# en 113 mensajes -- la etiqueta, no el dato.
_OTRA_ETIQUETA = (r"correo|email|mail|monto|banco|c[eé]dula|cedula|dni|tipo|cuenta|"
                  r"n[uú]mero|numero|nro|agencia|usuario|user|perfil|jugador|agente|"
                  r"celular|tel[eé]fono|telefono|si|para|el|la|los|tu|su|de")

# NINGUNA palabra del valor puede ser otra etiqueta. El lookahead va tambien sobre la
# PRIMERA porque el valor puede cruzar el salto de linea, y sin el `Nombres:` se comia el
# `Apellidos:` de abajo. Sobre las que siguen resuelve el caso de una sola linea:
# `Nombre: Mendoza Lopez Luiggi Cedula: 131...` corta antes de `Cedula` y la fila
# sigue siendo legible.
_PAL_NOMBRE = rf"(?![{_LETRA}]+[ \t]*[:=])[{_LETRA}]{{2,}}"
_PAL1_NOMBRE = rf"(?!(?:{_OTRA_ETIQUETA})\b){_PAL_NOMBRE}"
_VALOR_NOMBRE = rf"{_PAL1_NOMBRE}(?:[ \t]+{_PAL_NOMBRE}){{0,3}}"

# EL SEPARADOR SIEMPRE LLEVA `:` o `=`, y es lo unico que distingue un campo de la prosa.
# Se probo aflojarlo y el resultado fue el que decide: sin separador se cubrian 24
# mensajes mas, pero se rompia la idempotencia en 102 y se comian 20 etiquetas. El
# `\*?` es el asterisco de negrita de WhatsApp, que viaja ENTRE los dos puntos y el
# valor (`*Nombre:* Katty ...`) y sin el se escapaba la plantilla de banco entera.
_SEP_NOMBRE = r"[ \t]*[:=][ \t]*\*?[ \t]*(?:\n[ \t]*\*?[ \t]*)?"

_NOMBRE = re.compile(
    rf"((?:{_ET_NOMBRE}){_NO_PERSONA}[^:=\n]{{0,12}}?{_SEP_NOMBRE})({_VALOR_NOMBRE})",
    re.IGNORECASE)

# LA ETIQUETA SIN SEPARADOR, pero ANCLADA AL ARRANQUE DE LA LINEA (`Nombres Gema Moran
# Espinales`). Suelta seria veneno: "el titular enviado no deja que veamos" no lleva dos
# puntos y se comeria la frase. Anclada, la prosa no entra porque no ARRANCA con la
# etiqueta, y lo que queda despues de sacarla pasa por el mismo piso de dos palabras.
_ET_EN_LINEA = re.compile(
    rf"^[ \t*]*(?:{_ET_NOMBRE}){_NO_PERSONA}[ \t.:=*]+(?=[{_LETRA}])", re.IGNORECASE)

# UNA LINEA QUE ES SOLO PALABRAS. De dos a seis: seis entra por los nombres compuestos
# ("Washington Ariel Villavicencio De La Guerra") y no agrego ningun falso al medirlo.
_LINEA_SOLO_PALABRAS = re.compile(
    rf"^[ \t*]*([{_LETRA}]{{2,}}(?:[ \t]+[{_LETRA}]{{2,}}){{1,5}})[ \t*]*$")

# EL VOCABULARIO DEL FORMULARIO. Es lo que separa `Eduardo Vinicio Vega Chavez` de `Banco
# Pichincha`, `Cuenta de ahorro` o `Buen dia`, que tienen exactamente la misma forma. Sin
# esta lista el heuristico tapaba una de cada tres lineas por nada.
_VOCAB_FORMULARIO = re.compile(
    r"^(?:"
    r"nombres?|nombr[eé]s?|apellidos?|titular|c[eé]dula|cedula|dni|documento|"
    r"cuenta|cuentas|cueta|cta|ctas|ahorro|ahorros|corriente|transaccional|tipo|"
    r"n[uú]mero|numero|nro|monto|valor|retiro|retirar|dep[oó]sito|deposito|depositar|"
    r"agencia|ag|usuario|user|perfil|jugador|jugadores|agente|correo|celular|tel[eé]fono|"
    r"abono|abonos|boleta|boletas|pendiente|pendientes|oficial|p[aá]gina|pagina|alterna|"
    r"banco|bancaria|bancario|pichincha|guayaquil|pac[ií]fico|pacifico|produbanco|"
    r"bolivariano|austro|internacional|jep|coopmego|cooperativa|loja|machala|"
    r"buen|buena|buenas|buenos|d[ií]a|d[ií]as|tarde|tardes|noche|noches|hola|gracias|"
    r"favor|porfavor|porfa|amigo|amiga|bro|se[nñ]or|se[nñ]ora|por|para|de|del|la|el|"
    r"los|las|un|una|otro|otra|mi|tu|su|es|y|o|con|sin|solo|ya|no|si|s[ií]|aqui|aqu[ií]"
    r")$", re.IGNORECASE)

# Los conectores de un nombre compuesto no lo descalifican, pero tampoco cuentan: hacen
# falta DOS palabras reales, y ninguna puede abrir la linea.
_CONECTOR = re.compile(r"^(?:de|del|la|las|los|el|da|dos)$", re.IGNORECASE)

# LA LINEA DE ARRIBA MANDA. `Nombre de Agencia` en una linea y `SEPY BET` en la de abajo
# es un nombre COMERCIAL. Es el mismo criterio que `_NO_PERSONA`, cruzando el salto.
_ARRIBA_NO_PERSONA = re.compile(r"(?:agencia|usuario|user|perfil|jugador|banco)[ \t*:]*$",
                                re.IGNORECASE)

# EL HEURISTICO SOLO CORRE DONDE HAY UN FORMULARIO. En un chat comun dos palabras seguidas
# son dos palabras. Se probo exigir cedula Y cuenta a la vez y NO mejoro la precision
# (2,8% de falsos en las dos variantes): solo capturaba la mitad de los nombres reales.
_CTX_DIGITOS = re.compile(r"\d{6,}")
_CTX_FORMULARIO = re.compile(r"c[eé]dula|cedula|banco|cuenta|monto|retirar|titular|dni",
                             re.IGNORECASE)


def _enmascarar_nombre(valor: str) -> str:
    """`Juan Perez` -> `J*** P****`. Cada palabra conserva su INICIAL.

    No reusa `enmascarar` a proposito. Un nombre no es un identificador de largo fijo: la
    inicial es lo que deja al supervisor seguir a la misma persona a lo largo del chat sin
    saber quien es, y con `enmascarar` una palabra de cuatro letras se iria entera a
    asteriscos. El largo se conserva igual que en el resto del modulo.
    """
    return " ".join(p[0] + "*" * (len(p) - 1) if len(p) > 1 else p
                    for p in valor.split())


def _censurar_nombre_suelto(texto: str) -> str:
    """La linea que es SOLO un nombre, dentro de un mensaje con forma de formulario."""
    if not (_CTX_DIGITOS.search(texto) and _CTX_FORMULARIO.search(texto)):
        return texto
    salida, anterior = [], ""
    for linea in texto.split("\n"):
        candidata, et = linea, _ET_EN_LINEA.match(linea)
        if et:
            candidata = linea[et.end():]          # la etiqueta se queda, el valor se tapa
        elif _ARRIBA_NO_PERSONA.search(anterior):
            salida.append(linea)
            if linea.strip():
                anterior = linea
            continue
        m = _LINEA_SOLO_PALABRAS.match(candidata)
        if m:
            palabras = m.group(1).split()
            reales = [p for p in palabras if not _CONECTOR.match(p)]
            if (len(reales) >= 2 and not _CONECTOR.match(palabras[0])
                    and not any(_VOCAB_FORMULARIO.match(p) for p in reales)):
                inicio = linea.rindex(m.group(1))
                linea = linea[:inicio] + _enmascarar_nombre(m.group(1)) + \
                    linea[inicio + len(m.group(1)):]
        salida.append(linea)
        if linea.strip():
            anterior = linea
    return "\n".join(salida)


def censurar_texto(texto: str | None) -> str:
    """El texto con el dato sensible enmascarado. Puro, idempotente, sin estado.

    IDEMPOTENTE: la salida puede volver a pasar por aca (un endpoint que compone otro sin
    saber si ya paso). Los asteriscos no matchean ninguno de los patrones, asi que
    enmascarar dos veces no se come mas caracteres. Hay un test que lo fija.
    """
    if not texto:
        return ""
    out = _CRED_DOS_PUNTOS.sub(lambda m: m.group(1) + enmascarar(m.group(2)), texto)
    out = _CRED_ES.sub(lambda m: m.group(1) + enmascarar(m.group(2)), out)
    # LOS NOMBRES VAN ANTES QUE LOS DIGITOS, y el orden no es estetico: el heuristico de
    # linea decide si hay un formulario mirando la cedula y la cuenta. Enmascarados
    # primero, ya no los encuentra y la regla se apaga sola.
    out = _NOMBRE.sub(lambda m: m.group(1) + _enmascarar_nombre(m.group(2)), out)
    out = _censurar_nombre_suelto(out)
    out = _CORREO.sub(lambda m: enmascarar(m.group(1)) + m.group(2), out)
    out = _TELEFONO.sub(lambda m: enmascarar(m.group(0)), out)
    out = _DIGITOS.sub(lambda m: enmascarar(m.group(0)), out)
    return out
