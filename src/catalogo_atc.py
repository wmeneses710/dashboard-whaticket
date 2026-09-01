"""El vocabulario de ATC, tal como ellos lo escribieron. Es la fuente de verdad del tablero.

POR QUE EXISTE ESTE MODULO. Hoy `errores[]` lo llena el LLM en TEXTO LIBRE, y eso lo vuelve
inservible para lo unico que el negocio quiere hacer con el: contar. MEDIDO el 2026-08-19
sobre la copia: **7.019 errores emitidos en 3.680 TEXTOS DISTINTOS (52% unicos)**. El mismo
error aparece escrito de cinco formas:

    "No se pidieron los datos necesarios para crear la cuenta."          464
    "No se solicito al cliente los datos necesarios para crear la cuenta." 115
    "No se pidio al cliente los datos necesarios para crear la cuenta."   115
    "No se le pidio al cliente los datos necesarios para crear la cuenta." 49
    "No se pidieron los datos necesarios para crear la cuenta"             56

Son la misma falta contada cinco veces, y ninguna se puede comparar entre operadores ni
sumar en un cuadro. Un supervisor no puede decir "esta semana tuvimos 40 de este error".

LA SOLUCION NO LA INVENTAMOS NOSOTROS: EL NEGOCIO YA LA ESCRIBIO. El manual de ATC publica
dos listas CERRADAS y NUMERADAS -- doce errores criticos y doce buenas practicas -- y aclara
que cualquiera de los errores "puede derivar en medidas correctivas". Esa es la rubrica del
negocio, con las palabras del negocio. El tablero tiene que hablar ese idioma, no el nuestro.

REGLA DE ESTE ARCHIVO: **el campo `texto` es VERBATIM del manual y no se edita.** Si el
manual cambia, se actualiza aca y se dice de que version del manual sale. Lo que si es
nuestro es `chip` -- la version corta para la pantalla -- y `senal`, que apunta a la funcion
determinista que ya sabe detectarlo cuando existe.

Manual de ATC de Sorti365, cap. 01, secciones "Errores criticos en la atencion que no deben
ocurrir" y "Buenas practicas esenciales que deben estar siempre presentes".
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Falta:
    """Un error critico del manual. `codigo` es su numero en la lista de ellos."""

    codigo: str
    chip: str      # etiqueta corta para el tablero (nuestra)
    texto: str     # VERBATIM del manual -- no editar
    detalle: str   # la explicacion que el propio manual da debajo del titulo
    senal: str | None = None   # funcion determinista que ya lo detecta, si existe


@dataclass(frozen=True)
class Practica:
    """Una buena practica del manual. TIENE DOS CARAS y las dos hacen falta.

    `chip` es el LOGRO, en pasado: "cumplió los tiempos". Es para `aciertos[]`, donde se
    reconoce lo que la persona YA hizo bien.

    `foco` es el OBJETIVO, en infinitivo: "cumplir los tiempos". Es para el coaching, que
    apunta a la practica que FALTA trabajar.

    UN SOLO TEXTO NO ALCANZA, y costo un bug reportado por el negocio el 2026-09-01: en una
    nota de 2 estrellas por tardar, debajo de **Recomendacion**, salia un chip verde que
    decia "B10 cumplió los tiempos". La rubrica lo castigaba por lento y la pantalla lo
    felicitaba por rapido, en la misma tarjeta. El sentido se invierte segun el contexto.
    """

    codigo: str
    chip: str                  # el LOGRO, en pasado -- para `aciertos[]`
    texto: str                 # VERBATIM del manual -- no editar
    senal: str | None = None   # funcion determinista que ya lo detecta, si existe
    foco: str = ""             # el OBJETIVO, en infinitivo -- para el coaching


# --- LOS DOCE ERRORES CRITICOS ----------------------------------------------------
# La numeracion es la DEL MANUAL. No reordenar: el supervisor los conoce por su numero.
ERRORES: tuple[Falta, ...] = (
    Falta("E01", "no leyó el mensaje",
          "Responder sin leer o interpretar correctamente el mensaje del cliente.",
          "Esto genera confusión, retrasa la gestión y demuestra falta de profesionalismo."),
    Falta("E02", "pidió un dato repetido",
          "Solicitar información que el cliente ya proporcionó.",
          "Aumenta la frustración y evidencia falta de atención."),
    Falta("E03", "respuesta ambigua o inventada",
          "Dar respuestas ambiguas, incorrectas o inventadas.",
          "El operador nunca debe adivinar, improvisar o asumir información."),
    Falta("E04", "respuesta reactiva",
          "Responder de manera reactiva, impulsiva o con falta de respeto.",
          "Sin excepción, está prohibido involucrarse emocionalmente, discutir o hacer "
          "comentarios personales.",
          senal="operator_maltrato"),
    Falta("E05", "prometió algo no permitido",
          "Prometer soluciones que no están permitidas por los procesos internos.",
          "No se puede ofrecer recargas, bonos, devoluciones u otros beneficios sin "
          "autorización."),
    Falta("E06", "cerró sin despedida",
          "Cerrar chats sin seguimiento adecuado o sin despedida.",
          "Cada conversación debe cerrarse con un mensaje claro, cordial y profesional.",
          senal="cliente_tuvo_la_ultima_palabra"),
    Falta("E07", "transfirió sin avisar",
          "Transferir un chat sin notificar al cliente.",
          "El cliente debe saber que otro operador continuará su atención para evitar "
          "confusiones."),
    Falta("E08", "caso pendiente sin nota",
          "Dejar casos pendientes sin nota interna o sin observación.",
          "Todo caso debe quedar documentado para que cualquier operador pueda retomarlo."),
    Falta("E09", "no respetó los tiempos",
          "No respetar los tiempos de espera y seguimiento establecidos.",
          "Especialmente los mensajes a los 15 y 30 minutos cuando el cliente no responde."),
    Falta("E10", "alteró una respuesta rápida",
          "Alterar respuestas rápidas, protocolos o información oficial.",
          "Está estrictamente prohibido modificar textos aprobados o adaptar procesos por "
          "cuenta propia."),
    Falta("E11", "no verificó datos esenciales",
          "Atender de forma apresurada o sin verificar datos esenciales.",
          "Esto puede generar errores en cargas, retiros, validaciones o montos."),
    Falta("E12", "lenguaje fuera del estándar",
          "Usar lenguaje informal, irónico, sarcástico o fuera del estándar profesional.",
          "La comunicación debe ser impecable, incluso ante clientes difíciles."),
)

# --- LAS DOCE BUENAS PRACTICAS ----------------------------------------------------
# El espejo positivo. `derive_aciertos` (src/rubrics.py) ya arma una lista de lo que se
# hizo BIEN con claves NUESTRAS (resolucion/claridad/iniciativa/cortesia); estas son las
# de ellos, y son las que el supervisor reconoce.
PRACTICAS: tuple[Practica, ...] = (
    Practica("B01", "leyó todo antes de responder",
             "Leer y analizar completamente cada mensaje antes de responder.",
             foco="leer todo antes de responder"),
    Practica("B02", "respondió claro y ordenado",
             "Responder de forma clara, directa y ordenada.",
             foco="responder claro y ordenado"),
    Practica("B03", "tono cordial y empático",
             "Mantener un tono cordial, profesional y empático en todo momento.",
             foco="sostener el tono cordial"),
    Practica("B04", "usó el nombre del cliente",
             "Usar el nombre del cliente o su seudónimo cuando sea apropiado.",
             foco="usar el nombre del cliente"),
    Practica("B05", "verificó el dato sensible",
             "Verificar dos veces cualquier dato sensible antes de proceder.",
             foco="verificar el dato sensible"),
    Practica("B06", "documentó con nota interna",
             "Documentar todas las acciones con notas internas claras.",
             foco="documentar con nota interna"),
    Practica("B07", "usó la respuesta rápida correcta",
             "Utilizar las respuestas rápidas correctas sin modificar su contenido.",
             foco="usar la respuesta rápida correcta"),
    Practica("B08", "aplicó las etiquetas",
             "Aplicar etiquetas adecuadas y mantenerlas actualizadas.",
             foco="aplicar las etiquetas"),
    Practica("B09", "avisó la transferencia",
             "Informar al cliente cuando su caso será transferido.",
             foco="avisar la transferencia"),
    Practica("B10", "cumplió los tiempos",
             "Cumplir con los tiempos de respuesta establecidos.",
             senal="espera <= AGIL",
             foco="cumplir los tiempos"),
    Practica("B11", "mantuvo el control emocional",
             "Mantener siempre el control emocional.",
             foco="mantener el control emocional"),
    Practica("B12", "cerró bien el chat",
             "Cerrar cada chat de forma correcta y profesional.",
             senal="operator_asked_and_waited",
             foco="cerrar bien el chat"),
)

# --- LAS RESPUESTAS RAPIDAS -------------------------------------------------------
# El catalogo que el manual nombra. Sirve para escribir el coaching en su jerga: "usa
# R5PLACER" dice mas que "manda un mensaje de seguimiento", porque el operador la tiene en
# Whaticket y sabe cual es.
#
# LAS GRAFIAS SALEN DEL CRM, NO DEL MANUAL (corregido el 2026-08-28). Hasta hoy estaban
# transcritas a mano desde el manual, con una barra delante y en minusculas, y NINGUNA
# coincidia con la que el operador ve en su lista:
#
#     lo que mostrabamos            lo que el operador tiene en Whaticket
#     /R2verificaciondeboleta       R2VERIFICACIONDEBOLETA
#     /R3Recarga                    R3RECARGA
#     /Bienvenida                   BIENVENIDA
#     /contacto no registrado       CONTACTO NO REGISTRADO
#     /VerificarCuenta              VERIFICARCUENTA
#     /R1solicituddecarga           R1SOLICITUDDECARGA
#     /FIN                          FIN
#     /R5Placer                     R5PLACER
#     /Visto                        VISTO
#     /Link afiliado nuevo jugador  NO EXISTE
#
# El sentido de nombrarlas era "no dejar nada que interpretar"; con la grafia mal dejaba
# TODO que interpretar -- el operador la busca y no la encuentra, y encima eso es el propio
# error critico E10 ("alterar respuestas rapidas... o informacion oficial").
#
# LA BARRA NO ES UN PREFIJO UNIFORME. En el catalogo real hay shortcuts CON barra (`/000`,
# `/888ALE`, `/agenteverificacion`, `/tuverificacion`) y sin ella (`FIN`, `BIENVENIDA`,
# `R3RECARGA`). Ponerla o quitarla "para que se lea mejor" es inventar un nombre.
# Se copian VERBATIM, y el contrato lo ata tests/test_catalogo_atc_contra_el_crm.py contra
# `fast_responses` -- el unico chequeo que no es circular.
RESPUESTAS_RAPIDAS: dict[str, str] = {
    "BIENVENIDA": "saludo inicial, para no pasarse del minuto mientras se arma la respuesta",
    "CONTACTO NO REGISTRADO": "pedir los datos del cliente de forma estructurada",
    "VERIFICARCUENTA": "guiar la verificación de la cuenta",
    "R1SOLICITUDDECARGA": "acusar la solicitud de carga, que queda en proceso",
    "R2VERIFICACIONDEBOLETA": "avisar que se está verificando el comprobante",
    # NO es "en curso", y la diferencia importa: su texto real dice "Tu saldo ya está
    # disponible", o sea la acreditacion CONSUMADA. Aconsejar mandarla mientras la carga
    # sigue en curso le miente al cliente Y hace que `signals.operator_acreditacion` marque
    # acredito=True sin que la plata haya entrado (medido: la senal la lee como ACREDITO).
    # El momento "en curso" lo cubren R1SOLICITUDDECARGA y R2VERIFICACIONDEBOLETA.
    "R3RECARGA": "confirmar que el saldo YA quedó acreditado",
    "FIN": "despedida, una vez resuelta la solicitud",
    "R5PLACER": "cierre tras la espera, o seguimiento del cliente que no responde",
    "VISTO": "cerrar el chat después de los 5 minutos de espera",
}
# SACADA: "/Link afiliado nuevo jugador" ("enviar el enlace oficial de registro"). El manual
# la nombra y el CRM NO la tiene con ningun nombre parecido. Los dos candidatos por
# CONTENIDO son `A1.1JG` ("Regístrate en Sorti365... Link de registro") en `sistemas` y
# `LINK SORTIGO` (solo la URL) en `datos`, pero ninguno se llama asi, y elegir uno seria
# inventar un mapeo que el manual no hizo. Se saca en vez de adivinar: mostrar en el tablero
# un shortcut que no existe es peor que no mostrarlo. PENDIENTE de que el negocio confirme si
# `A1.1JG` es su sucesora.

# Indices por codigo, para que el tablero y las rubricas no recorran la tupla.
ERROR_POR_CODIGO: dict[str, Falta] = {f.codigo: f for f in ERRORES}
PRACTICA_POR_CODIGO: dict[str, Practica] = {p.codigo: p for p in PRACTICAS}

CODIGOS_ERROR: tuple[str, ...] = tuple(f.codigo for f in ERRORES)
CODIGOS_PRACTICA: tuple[str, ...] = tuple(p.codigo for p in PRACTICAS)


def texto_de_error(codigo: str) -> str:
    """La frase del manual para un codigo. Un codigo desconocido se devuelve tal cual: el
    tablero prefiere mostrar algo raro antes que romperse, y asi un codigo viejo de una
    corrida anterior sigue siendo legible."""
    f = ERROR_POR_CODIGO.get(codigo)
    return f.texto if f else codigo


def chip_de_error(codigo: str) -> str:
    """La etiqueta corta para la pantalla."""
    f = ERROR_POR_CODIGO.get(codigo)
    return f.chip if f else codigo


def bloque_para_el_prompt() -> str:
    """La lista cerrada, formateada para el system prompt del pase con LLM.

    Se le da al modelo CON el numero y la frase del manual: el numero es lo que el
    supervisor reconoce, y la frase completa es lo que le fija el criterio al modelo. Sin la
    frase, un codigo suelto es una etiqueta que cada corrida interpreta distinto.
    """
    return "\n".join(f"- {f.codigo}: {f.texto}" for f in ERRORES)


def bloque_practicas_para_el_prompt() -> str:
    """Las doce buenas practicas, formateadas para el system prompt.

    Espejo de `bloque_para_el_prompt` y por la MISMA razon: se le dan al modelo con el numero
    Y la frase del manual. El numero es lo que el supervisor reconoce; la frase es lo que le
    fija el criterio al modelo -- sin ella, un codigo suelto es una etiqueta que cada corrida
    interpreta distinto.
    """
    return "\n".join(f"- {p.codigo}: {p.texto}" for p in PRACTICAS)
