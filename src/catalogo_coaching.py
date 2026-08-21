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

# Las otras seis rubricas entran aca, mecanicamente y en su propio commit: deposito (22
# textos), promo (16), registro (16), soporte (16), info (13), retiro (11). Se agregan con
# el mismo par de tests que ata `agilidad` -- que el texto emitido SEA el del catalogo y que
# el codigo viaje en la fila -- para que no queden dos fuentes de verdad.
CONSEJOS: tuple[Consejo, ...] = _AGILIDAD


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
