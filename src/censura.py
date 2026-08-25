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

LOS NOMBRES TAMPOCO, y es una decision PENDIENTE del negocio. El nombre del operador es
el eje del tablero (sin el no hay a quien coachear), y el del cliente no se puede detectar
de forma confiable en texto libre sin una lista de nombres -- una heuristica de "esto
parece un nombre" produciria falsos que taparian palabras comunes. Si el negocio quiere
tapar el nombre del cliente, la via honesta es la columna (`contacts.name`), no el regex.
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
    out = _CORREO.sub(lambda m: enmascarar(m.group(1)) + m.group(2), out)
    out = _TELEFONO.sub(lambda m: enmascarar(m.group(0)), out)
    out = _DIGITOS.sub(lambda m: enmascarar(m.group(0)), out)
    return out
