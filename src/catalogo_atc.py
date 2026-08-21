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
    """Una buena practica del manual: el espejo positivo, para `aciertos[]`."""

    codigo: str
    chip: str
    texto: str
    senal: str | None = None


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
             "Leer y analizar completamente cada mensaje antes de responder."),
    Practica("B02", "respondió claro y ordenado",
             "Responder de forma clara, directa y ordenada."),
    Practica("B03", "tono cordial y empático",
             "Mantener un tono cordial, profesional y empático en todo momento."),
    Practica("B04", "usó el nombre del cliente",
             "Usar el nombre del cliente o su seudónimo cuando sea apropiado."),
    Practica("B05", "verificó el dato sensible",
             "Verificar dos veces cualquier dato sensible antes de proceder."),
    Practica("B06", "documentó con nota interna",
             "Documentar todas las acciones con notas internas claras."),
    Practica("B07", "usó la respuesta rápida correcta",
             "Utilizar las respuestas rápidas correctas sin modificar su contenido."),
    Practica("B08", "aplicó las etiquetas",
             "Aplicar etiquetas adecuadas y mantenerlas actualizadas."),
    Practica("B09", "avisó la transferencia",
             "Informar al cliente cuando su caso será transferido."),
    Practica("B10", "cumplió los tiempos",
             "Cumplir con los tiempos de respuesta establecidos.",
             senal="espera <= AGIL"),
    Practica("B11", "mantuvo el control emocional",
             "Mantener siempre el control emocional."),
    Practica("B12", "cerró bien el chat",
             "Cerrar cada chat de forma correcta y profesional.",
             senal="operator_asked_and_waited"),
)

# --- LAS RESPUESTAS RAPIDAS -------------------------------------------------------
# El catalogo que el manual nombra. Sirve para DOS cosas: escribir el coaching en su jerga
# ("usa /R5PLACER" dice mas que "manda un mensaje de seguimiento"), y para auditar el error
# E10 el dia que el negocio nos pase el TEXTO CANONICO de cada una -- que el manual NO
# incluye, y sin el "no alterar la plantilla" no se puede medir.
RESPUESTAS_RAPIDAS: dict[str, str] = {
    "/Bienvenida": "saludo inicial, para no pasarse del minuto mientras se arma la respuesta",
    "/contacto no registrado": "pedir los datos del cliente de forma estructurada",
    "/VerificarCuenta": "guiar la verificación de la cuenta",
    "/Link afiliado nuevo jugador": "enviar el enlace oficial de registro",
    "/R1solicituddecarga": "acusar la solicitud de carga",
    "/R2verificaciondeboleta": "avisar que se está verificando el comprobante",
    "/R3Recarga": "confirmar que la recarga está en curso",
    "/FIN": "despedida, una vez resuelta la solicitud",
    "/R5Placer": "cierre tras la espera, o seguimiento del cliente que no responde",
    "/Visto": "cerrar el chat después de los 5 minutos de espera",
}

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
