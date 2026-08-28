"""Que respuesta rapida uso el operador, contra el catalogo real del CRM.

POR QUE EXISTE APARTE DE `src/plantillas.py`. Ese modulo decide por CANAL (`sent_from`), y el
2026-08-28 quedo probado que el canal NO ve las respuestas rapidas: de 14.789 mensajes que
llamaba plantilla, **CERO** matchean una plantilla real del catalogo. Lo que hay en ese canal
son los AUTOMATICOS (saludo de cola, despedida de conexion, campañas). Las respuestas rapidas
salen por `WEB`, mezcladas con el texto libre, y la unica forma de reconocerlas es por TEXTO.

Y por texto funciona en CUALQUIER canal. Eso es lo que destapa Facebook, donde `sent_from`
nunca es NULL y el detector por canal es completamente ciego.

QUE HABILITA. El manual de ATC nombra sus respuestas rapidas catorce veces y no transcribe
ninguna, asi que el error critico **E10** ("alterar respuestas rapidas, protocolos o
informacion oficial") y la buena practica **B07** ("usar las respuestas rapidas correctas sin
modificar su contenido") estaban inmedibles. El ETL ahora trae el texto (`fast_responses`,
178 con texto en `sistemas`), y con el se puede.

EL CRITERIO DE ADMISION SALE DEL DATO, medido sobre 12.000 mensajes del operador de 30 dias:

    criterio                plantillas   msgs tocados   pierde
    largo total >= 60           163       2.302 (19,2%)   --
    tramo literal >= 25         177       2.307 (19,2%)   --
    tramo literal >= 35         174       2.307 (19,2%)   --      <- elegido
    tramo literal >= 45         165       2.221 (18,5%)   FIN

Se mide el TRAMO LITERAL y no el largo total porque `/FIN` son 64 caracteres de los cuales
**20 son `{{contactTreatment}}`**: con umbral sobre el largo total se descarta justo la
plantilla que el manual nombra primero. 45 la pierde igual; 35 conserva las seis del manual
sin aflojar la cobertura.

ESTE MODULO NO LO CONSUME NINGUNA RUBRICA TODAVIA, a proposito y por la misma leccion que
dejo `plantillas.py`: primero la señal medida, despues el cableado. Y antes de cablearlo hay
que resolver una cosa que el modulo NO puede resolver solo: **el catalogo es el estado de HOY
y los mensajes son historicos**. `fast_responses.updated_at` viaja en el mapa para que el
llamador decida; el texto de una version anterior de la plantilla no se puede reconstruir.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from src.operators import nombre_de_firma

# Minimo de caracteres LITERALES seguidos que tiene que tener una plantilla para entrar al
# mapa. Ver la tabla del docstring: es lo que separa una plantilla distintiva de un patron
# que matchearia prosa cualquiera.
TRAMO_MINIMO = 35

# Los `{{placeholders}}` del CRM: `{{contactTreatment}}`, `{{greeting}}`, `{{contactName}}`.
# La plantilla GUARDADA los lleva; la ENVIADA los tiene sustituidos, asi que el match exacto
# falla en 45 de las 178. Se compilan a comodin.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*\w+\s*\}\}")
# Acotado y no-codicioso: un `.*` dejaria que dos plantillas distintas se pisen, y el `\n`
# afuera para que un comodin no se coma media conversacion.
_HUECO = r"[^\n]{0,40}?"

# El prefijo de firma que el CRM agrega al mensaje del operador: `*Michelle:* <texto>`.
# Se saca SOLO si `nombre_de_firma` confirma que es el nombre de una PERSONA. Sin ese
# filtro se come el encabezado de las plantillas ("Monto a retirar:", "Te llevas:"), que
# cumple las mismas guardas — la trampa ya anotada en src/operators.py, y la que me hizo
# reportar 35,3% de cobertura cuando el real es 19,2%.
_FIRMA_ASTERISCOS_RE = re.compile(r"^\*([^:*\n]{2,40}):\*\s*")


def _sin_firma(texto: str) -> str:
    """El cuerpo sin el prefijo de firma del operador, si lo que hay ES una firma."""
    m = _FIRMA_ASTERISCOS_RE.match(texto)
    if m and nombre_de_firma(f"*{m.group(1)}:*") is not None:
        return texto[m.end():]
    # El formato sin asteriscos (`Nombre:\n...`) se delega entero a `nombre_de_firma`, que
    # es quien sabe distinguirlo de un encabezado de plantilla.
    nombre = nombre_de_firma(texto)
    if nombre is not None:
        corte = texto.find(":", texto.find(nombre))
        if corte != -1:
            return texto[corte + 1:]
    return texto


def normalizar(body: str | None) -> str:
    """Texto comparable: sin firma, sin acentos, espacios colapsados, minusculas.

    NO toca los dos puntos ni los saltos convertidos en espacio: el encabezado del
    formulario de retiro ES parte de la plantilla y tiene que sobrevivir.
    """
    texto = _sin_firma((body or "").strip())
    descompuesto = unicodedata.normalize("NFD", texto)
    sin_acentos = "".join(c for c in descompuesto
                          if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sin_acentos).strip().lower()


def _tramos(normalizado: str) -> list[str]:
    """Los tramos LITERALES de la plantilla, o sea el texto entre placeholders."""
    return [p for p in _PLACEHOLDER_RE.split(normalizado) if p.strip()]


def compilar(message: str | None) -> tuple[re.Pattern, int] | None:
    """(regex de la plantilla, caracteres literales). None si no es lo bastante distintiva.

    El largo literal es lo que decide la ESPECIFICIDAD cuando dos plantillas matchean el
    mismo mensaje: gana la que aporta mas texto propio.
    """
    normalizado = normalizar(message)
    tramos = _tramos(normalizado)
    if not tramos or max(len(t) for t in tramos) < TRAMO_MINIMO:
        return None
    partes = [re.escape(p) for p in _PLACEHOLDER_RE.split(normalizado)]
    cuerpo = _HUECO.join(partes) if len(partes) > 1 else partes[0]
    try:
        return re.compile(cuerpo), sum(len(t) for t in tramos)
    except re.error:
        # Una plantilla que no compila no puede tumbar el mapa entero.
        return None


def build_plantillas_map(cur, account: str | None = None) -> dict[str, dict]:
    """Mapa shortcut -> {regex, literal, texto, account, updated_at}, desde `fast_responses`.

    Mismo patron que `redireccion.build_lineas_map`: es un agregado sobre el corpus y no una
    funcion pura, asi que se construye una vez y viaja como argumento.

    Sin scope de cuenta por default. Las dos cuentas comparten operadores (siete personas
    tienen una fila en `users` por cuenta), asi que acotar de entrada esconderia la plantilla
    que la persona conoce de su otra cuenta. Quien necesite el scope lo pide.
    """
    where = " WHERE account = %s" if account else ""
    cur.execute(
        "SELECT shortcut, message, account, updated_at FROM fast_responses"
        + where
        + " ORDER BY shortcut",
        (account,) if account else None,
    )
    mapa: dict[str, dict] = {}
    for shortcut, message, cuenta, updated_at in cur.fetchall():
        if not shortcut:
            continue
        compilada = compilar(message)
        if compilada is None:
            continue
        regex, literal = compilada
        mapa[shortcut] = {
            "regex": regex,
            "literal": literal,
            "texto": normalizar(message),
            "account": cuenta,
            "updated_at": updated_at,
        }
    return mapa


def _es_del_operador(message: dict) -> bool:
    """Mensaje del negocio al cliente. La nota del CRM es `from_me` pero no es un mensaje
    al cliente — misma leccion que `cliente_tuvo_la_ultima_palabra`."""
    return bool(message.get("from_me")) and not message.get("is_note")


def plantilla_de(message: dict, mapa: dict[str, dict] | None) -> str | None:
    """El shortcut de la respuesta rapida que el operador mando, o None.

    Sin mapa devuelve None: falla del lado seguro, igual que `lineas` en src/redireccion.py.
    Ante varias que matchean gana la MAS ESPECIFICA (mas texto literal): en el catalogo real
    `/michelle verificar comprobante` y `R2VERIFICACIONDEBOLETA` matchean los MISMOS 986
    mensajes, y la que explica mejor el mensaje es la que aporta mas texto propio.
    """
    if not mapa or not _es_del_operador(message):
        return None
    texto = normalizar(message.get("body"))
    if not texto:
        return None
    ganadora, mejor = None, -1
    for shortcut, entrada in mapa.items():
        if entrada["regex"].search(texto) and entrada["literal"] > mejor:
            ganadora, mejor = shortcut, entrada["literal"]
    return ganadora


def similitud(message: dict, shortcut: str, mapa: dict[str, dict] | None) -> float:
    """Cuanto se parece el mensaje a esa plantilla, entre 0 y 1. 0 si no se puede comparar.

    Los placeholders se neutralizan a un espacio antes de comparar: la plantilla guardada
    dice `{{contactTreatment}}` y la enviada trae el tratamiento real, y esa diferencia no
    es una alteracion del operador.
    """
    if not mapa or shortcut not in mapa:
        return 0.0
    texto = normalizar(message.get("body"))
    if not texto:
        return 0.0
    plantilla = _PLACEHOLDER_RE.sub(" ", mapa[shortcut]["texto"])
    plantilla = re.sub(r"\s+", " ", plantilla).strip()
    return SequenceMatcher(None, texto, plantilla).ratio()


def plantilla_mas_parecida(message: dict, mapa: dict[str, dict] | None,
                           minimo: float = 0.6) -> tuple[str | None, float]:
    """(shortcut de la plantilla que probablemente estaba usando, ratio). (None, 0.0) si
    ninguna llega al minimo.

    ES LO QUE E10 NECESITA, y `plantilla_de` no alcanza: un mensaje ALTERADO por construccion
    NO matchea su plantilla, asi que un `None` de `plantilla_de` no distingue "altero la
    plantilla" de "escribio libre". El ratio si: la version alterada queda alta y por debajo
    de 1, y el texto ajeno queda lejos.

    El minimo de 0,6 es el default de arranque y NO esta calibrado contra la data todavia:
    calibrarlo necesita una muestra etiquetada de alteraciones reales. Hasta entonces esto es
    una primitiva, no un veredicto.
    """
    if not mapa or not _es_del_operador(message):
        return None, 0.0
    ganadora, mejor = None, 0.0
    for shortcut in mapa:
        ratio = similitud(message, shortcut, mapa)
        if ratio > mejor:
            ganadora, mejor = shortcut, ratio
    if mejor < minimo:
        return None, mejor
    return ganadora, mejor
