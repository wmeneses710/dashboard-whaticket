"""El coaching determinista, declarado como catalogo cerrado y con codigo.

POR QUE EXISTE. Hoy la recomendacion se cuenta igual que se contaban los errores antes de
v21: no se puede. MEDIDO el 2026-08-21 sobre la copia -- 79.918 recomendaciones en 124.341
filas evaluadas -- el corte por autor parte el problema en dos mitades que no se parecen:

    determinista/agilidad-v1    22.076 recomendaciones ->      4 textos distintos
    determinista/deposito-v1    14.823                 ->     22
    determinista/promo-v1       12.165                 ->     16
    determinista/registro-v1     7.552                 ->     16
    determinista/soporte-v1      4.700                 ->     16
    determinista/info-v1         4.680                 ->     13
    determinista/retiro-v1       1.759                 ->     11
                                ------                     ----
                                67.755                 ->     98
    el LLM                      12.163                 -> 10.325   (84,9% unicos)

Las siete rubricas deterministas YA SON un catalogo cerrado y nadie lo habia declarado. Los
98 textos son ~38 bases por las combinaciones del apendice de `refine_recomendacion`: el
codigo identifica la BASE y los fragmentos llevan el suyo (ver FRAGMENTOS).

REGLA DE ESTE ARCHIVO, LA MISMA QUE `catalogo_atc.py`: **el campo `texto` es VERBATIM el que
la rubrica ya venia emitiendo.** Declarar no es reescribir. Si un texto hay que mejorarlo es
otro cambio, con su propia medicion -- si no, nunca se sabria si el numero se movio por el
catalogo o por la redaccion nueva.

CADA CONSEJO SE ATA A UNA BUENA PRACTICA DEL MANUAL (B01-B12, en catalogo_atc.PRACTICAS).
Eso es lo que lo vuelve auditable de punta a punta y en el idioma de ellos: el error dice
E0x, y el consejo dice a que practica apunta. Sin ese enganche seguimos hablando el nuestro.

LO QUE ESTE MODULO NO HACE. La recomendacion del LLM (12.163 filas, 84,9% de textos unicos)
sigue siendo prosa libre y NO entra aca: lo que corresponde ahi es que el modelo ELIJA de
B01-B12 en vez de escribir, y es un cambio de prompt con su propio test. Este archivo cubre
la mitad determinista, que es el 85% del volumen.

NUMERACION: `C##` estable y sin reordenar, igual que los E##. Un supervisor los va a conocer
por el numero, asi que un codigo no cambia de significado nunca: si un consejo se retira, su
numero queda libre y no se recicla.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Consejo:
    """Un consejo determinista. `situacion` es la rama de la rubrica que lo dispara."""

    codigo: str
    rubrica: str
    situacion: str        # la etiqueta o rama que lo elige (p. ej. "aceptable")
    chip: str             # etiqueta corta para el tablero (nuestra)
    texto: str            # VERBATIM lo que ya se emitia -- no editar
    practica: str         # el B## del manual al que apunta (catalogo_atc.PRACTICAS)


@dataclass(frozen=True)
class Fragmento:
    """Apendice determinista que `refine_recomendacion` agrega al consejo base."""

    codigo: str
    chip: str
    texto: str            # VERBATIM lo que ya se emitia -- no editar
    practica: str


# --- AGILIDAD (segmento agente) -------------------------------------------------------
# 22.076 recomendaciones con CUATRO textos: el 28% de todo el coaching del sistema.
# Los cuatro apuntan a B10 ("cumplir con los tiempos de respuesta establecidos") porque la
# rubrica mide UNA sola cosa por diseño: cuanto tardo el operador en cumplir el pedido.
# `excelente` no lleva consejo a proposito -- no hay nada que mejorar, e inventarle una
# falta para tener algo que mostrar es peor que el silencio.
_AGILIDAD: tuple[Consejo, ...] = (
    Consejo("C01", "agilidad", "mala", "no respondió el pedido",
            "Quedó un pedido sin responder. En operaciones de caja conviene contestar "
            "siempre, aunque sea con una línea avisando que ya se está procesando.",
            practica="B10"),
    Consejo("C02", "agilidad", "deficiente", "tardó más de 15 minutos",
            "La respuesta tardó más de 15 minutos. Son operaciones de rutina que no "
            "necesitan verificación: se puede avisar con /Bienvenida enseguida y "
            "confirmar al acreditar.",
            practica="B10"),
    Consejo("C03", "agilidad", "aceptable", "tardó más de 5 minutos",
            "La respuesta tardó más de 5 minutos. Si no se puede procesar en el momento, "
            "/Bienvenida alcanza para que el agente no quede esperando sin saber: el "
            "objetivo del manual es 1 minuto.",
            practica="B10"),
    Consejo("C04", "agilidad", "buena", "cerca del minuto",
            "Muy cerca del objetivo, que es responder dentro del minuto.",
            practica="B10"),
)

# --- INFO --------------------------------------------------------------------------------
# 4.680 recomendaciones. `info` selecciona por ESTRELLA, no por etiqueta como `agilidad`:
# la `situacion` refleja como elige la rubrica, no un formato unico. El contrato de
# `consejo_de` es que el llamador pase lo que usa para elegir.
_INFO: tuple[Consejo, ...] = (
    Consejo("C05", "info", "1", "nadie respondió la consulta",
            "El cliente preguntó y nadie le respondió. Conviene contestar aunque sea "
            "parcialmente: quien consulta todavía está decidiendo si se queda.",
            practica="B10"),
    Consejo("C06", "info", "2", "demoró la primera respuesta",
            "Quien pregunta todavía está decidiendo si se queda. Conviene responder con lo "
            "que se sabe y completar después, antes que demorar la primera respuesta.",
            practica="B10"),
    Consejo("C07", "info", "3", "pasó del minuto",
            "El objetivo es 1 minuto para la primera respuesta, aunque sea parcial: quien "
            "consulta está comparando y la demora se nota.",
            practica="B10"),
    Consejo("C08", "info", "4", "cerró sin preguntar si faltaba algo",
            "Cerrar con \"¿te falta algo más?\" rinde acá: en una consulta suele quedar una "
            "segunda duda sin plantear. Conviene esperar unos 5 minutos antes de cerrar el "
            "ticket: la pregunta solo sirve si el cliente alcanza a responderla.",
            practica="B12"),
)

# --- PROMO -------------------------------------------------------------------------------
# 12.165 recomendaciones. La rama de 4 estrellas NO esta: su texto se retiro el 2026-08-21
# por no tener respaldo en el manual (ver tests/test_coaching_sin_respaldo.py). Que falte una
# situacion es valido y `consejo_de` devuelve None.
_PROMO: tuple[Consejo, ...] = (
    Consejo("C09", "promo", "1", "nadie respondió la promo",
            "El cliente preguntó por la promo y nadie le respondió. Es la consulta con más "
            "intención de todas: conviene contestar aunque sea con lo que se sabe.",
            practica="B10"),
    Consejo("C10", "promo", "2", "esperó a tener todo el detalle",
            "Una consulta de promo se enfría rápido. Conviene responder aunque sea con lo "
            "que se sabe y completar después, en vez de esperar a tener todo el detalle.",
            practica="B10"),
    Consejo("C11", "promo", "3", "pasó del minuto",
            "En promo la ventana es corta: un primer mensaje dentro del minuto —aunque "
            "sea \"ya te confirmo el detalle\"— evita que la consulta se enfríe.",
            practica="B10"),
)


# --- LAS CUATRO RUBRICAS TRANSACCIONALES -------------------------------------------------
# Estas NO eligen por estrella: eligen por RAMA (ver el `_situacion()` de cada modulo). Una
# rama de rechazo con 4 estrellas no puede recibir el consejo del 4 normal -- el generico
# habla de crear la cuenta cuanto antes, que es justo lo que NO se podia hacer. Por eso la
# `situacion` nombra la rama y no el numero.
# Los textos se generaron LEYENDO las constantes de cada modulo, no transcribiendolos: los 24
# quedaron verificados verbatim contra el origen antes de mover nada.

_DEPOSITO: tuple[Consejo, ...] = (
    Consejo("C12", "deposito", "1", "el comprobante quedó sin respuesta",
            "El comprobante quedó sin respuesta. En caja conviene contestar siempre: "
            "una línea mientras se procesa evita que el cliente crea que se perdió su "
            "plata.",
            practica="B10"),
    Consejo("C13", "deposito", "2_sin_acreditar", "no confirmó que la plata entró",
            "Conviene confirmar que la plata entró con una línea al cierre: \"listo, ya "
            "tienes tu saldo\". Un \"en breve\" deja esa pregunta sin responder, y el "
            "cierre con /FIN recién corresponde cuando la gestión terminó.",
            practica="B12"),
    Consejo("C14", "deposito", "2_tarde", "tardó el primer aviso",
            "El primer aviso tardó demasiado. El manual separa los dos momentos y les "
            "da una respuesta rápida a cada uno: /R2verificaciondeboleta apenas entra "
            "el comprobante, y /R3Recarga cuando la carga ya está en curso.",
            practica="B07"),
    Consejo("C15", "deposito", "3", "no acusó el comprobante al entrar",
            "Un primer mensaje corto —\"ya lo recibí, lo reviso\"— apenas entra el "
            "comprobante alcanza para que el cliente no quede en silencio mientras se "
            "procesa.",
            practica="B10"),
    Consejo("C16", "deposito", "4", "cerró sin preguntar si faltaba algo",
            "Cerrar con \"¿te falta algo más?\" abre la puerta a la segunda duda, que en "
            "recargas suele ser el bono o el próximo depósito. Y conviene dar unos 5 "
            "minutos antes de cerrar el ticket: preguntar y cerrar en el mismo acto no "
            "deja tiempo de contestar.",
            practica="B12"),
    Consejo("C17", "deposito", "derivacion_rapida", "derivó rápido al agente",
            "La derivación salió rápido. Suma indicarle también que puede recargar "
            "desde la plataforma, así tiene la opción a mano si no ubica a su agente.",
            practica="B02"),
    Consejo("C18", "deposito", "derivacion_tarde", "tardó en derivar al agente",
            "Cuando la recarga le corresponde al agente, conviene decirlo enseguida y "
            "pasar su número: mientras espera, el cliente cree que su plata ya está en "
            "camino.",
            practica="B09"),
    Consejo("C19", "deposito", "rechazo_rapido", "avisó el rechazo rápido",
            "El aviso salió rápido. Lo que más ayuda es decirle también cómo "
            "arreglarlo —qué dato corregir o cómo verificar la cuenta— para que el "
            "próximo intento sí entre.",
            practica="B02"),
    Consejo("C20", "deposito", "rechazo_tarde", "tardó en avisar el rechazo",
            "El rechazo conviene avisarlo enseguida: mientras espera, el cliente cree "
            "que su plata está en camino. Un mensaje corto en cuanto se ve el problema "
            "evita esa espera a ciegas.",
            practica="B10"),
)

_RETIRO: tuple[Consejo, ...] = (
    Consejo("C21", "retiro", "1", "el pedido quedó sin respuesta",
            "El pedido de retiro quedó sin respuesta. Aunque no se pueda procesar en "
            "el momento, conviene acusar el recibo: el agente tiene plata comprometida.",
            practica="B10"),
    Consejo("C22", "retiro", "2_sin_comprobante", "no envió el comprobante",
            "Enviar el comprobante siempre, incluso si el agente no lo pidió: es el "
            "único respaldo de que la plata salió, y es lo que sostiene la confianza "
            "de la agencia.",
            practica="B02"),
    Consejo("C23", "retiro", "2_tarde", "el comprobante llegó tarde",
            "El comprobante llegó pero tarde. Conviene avisar en cuanto el retiro "
            "entra en proceso: el agente necesita saber que está en marcha, no solo "
            "que terminó.",
            practica="B10"),
    Consejo("C24", "retiro", "3", "no acusó el pedido en el minuto",
            "El objetivo es 1 minuto para acusar el pedido y 15 para tener el "
            "comprobante arriba. Acusar primero y entregar después cumple las dos "
            "cosas.",
            practica="B10"),
    Consejo("C25", "retiro", "4", "cerró sin preguntar si faltaba algo",
            "Cerrar con \"¿te falta algo más?\" es la diferencia entre entregar y "
            "acompañar: en retiro el agente suele tener una segunda operación en "
            "camino. Conviene dejar unos 5 minutos antes de cerrar el ticket, para que "
            "llegue a pedirla.",
            practica="B12"),
)

_REGISTRO: tuple[Consejo, ...] = (
    Consejo("C26", "registro", "1", "nadie respondió los datos",
            "El cliente entregó sus datos y nadie le respondió. Conviene acusar el "
            "recibo enseguida: ya había decidido registrarse y es el peor momento para "
            "dejarlo esperando.",
            practica="B10"),
    Consejo("C27", "registro", "2", "el alta quedó a medias",
            "El alta quedó a medias. Si la cuenta no se puede crear en el momento, "
            "conviene decirle cuándo la va a tener: ya entregó sus datos y está "
            "esperando.",
            practica="B02"),
    Consejo("C28", "registro", "3", "tardó más de 5 minutos en crear la cuenta",
            "El usuario y la clave tardaron más de 5 minutos desde que el cliente pasó "
            "sus datos. Es el momento de mayor riesgo de que se caiga: conviene crear "
            "la cuenta cuanto antes.",
            practica="B10"),
    Consejo("C29", "registro", "4", "no encaminó la primera recarga",
            "El alta salió rápido y ese es el momento de mayor intención del cliente. "
            "Conviene pasarle los medios de pago ahí mismo y no cerrar la conversación "
            "hasta que llegue el comprobante de la recarga.",
            practica="B02"),
    Consejo("C30", "registro", "rechazo_rapido", "avisó rápido que no se podía",
            "Avisaste rápido que la cuenta no se podía crear. Lo que suma es "
            "asegurarte de que llegue a quien sí puede ayudarlo: pasarle el contacto y "
            "verificar que lo recibió.",
            practica="B09"),
    Consejo("C31", "registro", "rechazo_tarde", "tardó en avisar que no se podía",
            "El aviso llegó tarde: el cliente estuvo esperando una cuenta que no iba a "
            "llegar. Cuando el alta no puede salir, conviene decirlo apenas se sabe.",
            practica="B10"),
)

_SOPORTE: tuple[Consejo, ...] = (
    Consejo("C32", "soporte", "1", "nadie respondió el problema",
            "El cliente reportó un problema con su cuenta y nadie le respondió. "
            "Conviene acusar el recibo aunque la solución dependa de otra área.",
            practica="B10"),
    Consejo("C33", "soporte", "2_sin_intento", "no dejó un paso a seguir",
            "El cliente no se llevó ningún paso a seguir. Aunque el desbloqueo dependa "
            "de otra área, conviene decirle qué sigue y en cuánto tiempo.",
            practica="B02"),
    Consejo("C34", "soporte", "3", "cada ida y vuelta tardó",
            "En soporte el cliente ya viene trabado y cada espera pesa doble. Un "
            "mensaje corto entre paso y paso alcanza para que no sienta que quedó solo.",
            practica="B10"),
    Consejo("C35", "soporte", "4", "cerró sin preguntar si faltaba algo",
            "Cerrar con \"¿te falta algo más?\" es el motivo donde más rinde, porque el "
            "problema de cuenta suele volver si quedó un paso a medias. Conviene "
            "esperar unos 5 minutos antes de cerrar el ticket, que es cuando aparece "
            "ese paso.",
            practica="B12"),
)

# LAS SIETE RUBRICAS DETERMINISTAS, COMPLETAS. Lo que queda afuera del catalogo es la
# recomendacion del LLM (12.163 filas, 84,9% de textos unicos): ahi corresponde que el
# modelo ELIJA de B01-B12 en vez de escribir, y es un cambio de prompt con su propio test.
CONSEJOS: tuple[Consejo, ...] = (
    _AGILIDAD + _INFO + _PROMO + _DEPOSITO + _RETIRO + _REGISTRO + _SOPORTE
)


# --- FRAGMENTOS de refine_recomendacion -----------------------------------------------
# El apendice tambien se cuenta: si el operador leyo "cambiale la contraseña", eso es una
# accion correctiva concreta y el tablero tiene que poder sumarla. Son los textos de
# src/recommendations.py, verbatim.
FRAGMENTOS: tuple[Fragmento, ...] = (
    Fragmento("F01", "avisar cambio de contraseña",
              "Como la cuenta se creó desde el operador, indícale al cliente que cambie "
              "la contraseña en su primer ingreso por seguridad.",
              practica="B05"),
    Fragmento("F02", "guiar a la web",
              "No hay app disponible por ahora; guía al cliente a usar la web (la app "
              "estará disponible próximamente).",
              practica="B02"),
)


CONSEJO_POR_CODIGO: dict[str, Consejo] = {c.codigo: c for c in CONSEJOS}
FRAGMENTO_POR_CODIGO: dict[str, Fragmento] = {f.codigo: f for f in FRAGMENTOS}

# (rubrica, situacion) -> Consejo. Se arma una vez: `consejo_de` se llama por cada sesion
# scoreada y recorrer la tupla en cada llamada no aporta nada.
_POR_SITUACION: dict[tuple[str, str], Consejo] = {
    (c.rubrica, c.situacion): c for c in CONSEJOS
}


def consejo_de(rubrica: str, situacion: str) -> Consejo | None:
    """El consejo de esa rama, o None si esa rama no lleva consejo.

    None NO es un error: `excelente` no tiene nada que mejorar, y una rubrica que todavia
    no se migro al catalogo tampoco. El llamador cae a su texto de siempre.
    """
    return _POR_SITUACION.get((rubrica, situacion))


def texto_de(codigo: str) -> str:
    """El texto de un codigo, para el tablero. Un codigo desconocido se devuelve tal cual:
    misma decision que `catalogo_atc.texto_de_error` -- una fila vieja con un codigo
    retirado tiene que seguir siendo legible antes que romper la pantalla."""
    c = CONSEJO_POR_CODIGO.get(codigo) or FRAGMENTO_POR_CODIGO.get(codigo)
    return c.texto if c else codigo


def chip_de(codigo: str) -> str:
    """La etiqueta corta para la pantalla."""
    c = CONSEJO_POR_CODIGO.get(codigo) or FRAGMENTO_POR_CODIGO.get(codigo)
    return c.chip if c else codigo
