"""Encolar un rescore PARCIAL por lista de identificadores.

POR QUE EXISTE ESTE MODULO Y NO VIVE EN EL SCRIPT. `scripts/` es la puerta de entrada y no
se testea; la decision de que se encola y que no si tiene que estar cubierta, porque el
error caro no es que falle: es que encole DE MENOS y nadie se entere.

LOS TRES IDENTIFICADORES Y SU RELACION, medidos sobre la copia (4.705 filas):

    ticket (831)  --1:N-->  sesion (1.064)  --1:N-->  interaccion (4.705)

  * `ticket_id` NUNCA es NULL: es una puerta de entrada valida.
  * Hacia arriba la relacion es ESTRICTA -- ninguna sesion pertenece a dos tickets
    (`max_tickets_por_sesion = 1`), asi que un id se resuelve sin ambiguedad.
  * Hacia abajo NO: un ticket llega a 31 sesiones y 167 interacciones. Marcar UN ticket
    puede encolar 167 filas, y eso hay que mostrarlo ANTES de escribir.

Y hay una amplificacion mas, aparte de esa: el worker scorea por SESION, asi que marcar una
sola interaccion rescorea todas las hermanas de su sesion.
"""
from __future__ import annotations

import re

# Cualquier separador razonable, mas los adornos que deja un copy-paste de psql, de un
# Excel o de una lista de Python: comillas, corchetes, parentesis. Nadie va a normalizar a
# mano lo que copia.
_SEPARADORES = re.compile(r"[\s,;|]+")
_ADORNOS = "[]()'\"`"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Las columnas donde puede vivir un id, EN ORDEN de especificidad: de lo mas fino a lo mas
# grueso. El orden importa para clasificar --un id se cuenta una sola vez, por la primera
# que matchea-- y para que el conteo que ve la persona no se infle.
COLUMNAS = (("interaccion", "interaccion_id"),
            ("sesion", "conversation_id"),
            ("ticket", "ticket_id"))


def parse_uuids(texto: str) -> tuple[list[str], list[str]]:
    """`(uuids validos sin repetir y en orden, pedazos que no son uuid)`.

    LO QUE NO ES UUID SE DEVUELVE APARTE Y NO SE TRAGA. Un id mal pegado tiene que VERSE:
    tragarlo en silencio encola de menos y deja a la persona creyendo que encolo todo.
    """
    vistos: dict[str, None] = {}
    basura: list[str] = []
    for pedazo in _SEPARADORES.split(texto or ""):
        limpio = pedazo.strip().strip(_ADORNOS)
        if not limpio:
            continue
        if _UUID_RE.match(limpio):
            vistos.setdefault(limpio.lower(), None)
        else:
            basura.append(limpio)
    return list(vistos), basura


def condicion_por_ids(ids: list[str]) -> tuple[str | None, list | None]:
    """El `WHERE` que cubre las tres columnas, con sus parametros.

    SE COMPARA LA COLUMNA COMO TEXTO, no el parametro como uuid. Los ids llegan de una caja
    de texto: casteando el parametro, un id con una letra de mas revienta el UPDATE ENTERO
    en vez de salir listado como `sin_match`. Con `::text` el id raro simplemente no matchea.

    SIN IDS DEVUELVE `None`, no un `WHERE true`: una condicion vacia encola la tabla entera,
    que es exactamente el rescore de 369 dias que todo esto viene a evitar.
    """
    if not ids:
        return None, None
    cond = " OR ".join(f"{col}::text = ANY(%s)" for _, col in COLUMNAS)
    return f"({cond})", [ids for _ in COLUMNAS]


def clasificar(cur, ids: list[str]) -> dict[str, list[str]]:
    """Que es cada id: `interaccion`, `sesion`, `ticket` o `sin_match`.

    UN ID SE CUENTA UNA SOLA VEZ, por la columna mas fina que lo matchea. No deberia haber
    colisiones --son espacios de nombres distintos-- pero si las hubiera, contarlo dos veces
    infla el numero con el que la persona decide si aplica o no.

    `sin_match` es lo mas probable que pase: un uuid de otra base, o de una fila que
    todavia no se scoreo. Sin reportarlo, el operador cree que encolo algo que no encolo.
    """
    pendientes = list(ids)
    out: dict[str, list[str]] = {nombre: [] for nombre, _ in COLUMNAS}
    for nombre, col in COLUMNAS:
        if not pendientes:
            break
        cur.execute(f"SELECT DISTINCT {col}::text FROM conversation_scores "  # noqa: S608
                    f"WHERE {col}::text = ANY(%s)", (pendientes,))
        encontrados = {f[0] for f in cur.fetchall()}
        out[nombre] = [i for i in pendientes if i in encontrados]
        pendientes = [i for i in pendientes if i not in encontrados]
    out["sin_match"] = pendientes
    return out


# --- EL ESTADO DE LA COLA -------------------------------------------------------------
#
# SOLO LECTURA, y eso es la mitad del punto. Antes, la unica forma de contar lo pendiente
# era correr `--deshacer` en seco: un `--aplicar` de mas ahi borra la cola entera. Preguntar
# como va algo no puede compartir comando con destruirlo.
#
# `pendientes` es EXACTAMENTE lo que el worker todavia debe: la misma condicion de "servida"
# (`scored_at >= rescore_pedido_at`) que lee `worker._notas_de_la_sesion`. Si ese numero no
# baja entre dos corridas, la cola no esta avanzando -- y eso es un bug, no una espera.
_ESTADO_SQL = """
SELECT count(*) FILTER (WHERE rescore_pedido_at > scored_at),
       count(DISTINCT conversation_id) FILTER (WHERE rescore_pedido_at > scored_at),
       count(*) FILTER (WHERE rescore_pedido_at <= scored_at),
       count(DISTINCT conversation_id) FILTER (WHERE rescore_pedido_at <= scored_at),
       max(scored_at) FILTER (WHERE rescore_pedido_at <= scored_at),
       max(rescore_pedido_at)
  FROM conversation_scores
 WHERE rescore_pedido_at IS NOT NULL
"""


def estado(cur) -> dict:
    """Cuanto falta y cuanto se hizo de los rescores pedidos. No escribe nada."""
    cur.execute(_ESTADO_SQL)
    f = cur.fetchone() or (0, 0, 0, 0, None, None)
    return {"pendientes": f[0], "pendientes_sesiones": f[1],
            "servidas": f[2], "servidas_sesiones": f[3],
            "ultima_servida": f[4], "ultimo_pedido": f[5]}
