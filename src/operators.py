"""Identidad del operador: reconstruir QUIEN atendio, desde el cuerpo de sus mensajes.

La firma en el mensaje NO es un fallback: es la UNICA fuente de historia. Verificado el
2026-08-07 contra la API viva del CRM — `GET /users` devuelve solo los usuarios VIVOS
(30 en sistemas, 15 en datos) y de los 38 operadores huerfanos devuelve **CERO**. El CRM
BORRA usuarios, y un usuario borrado tampoco se recupera de a uno (`GET /users/<borrado>`
da 401 `ERR_NO_USER_FOUND`, ver src/client.py del ETL). O sea que refrescar el catalogo no
trae nada: lo que no quedo escrito en un mensaje, se perdio.

Y cuando al CRM le recrean la cuenta a una persona, sus mensajes quedan repartidos entre
varios user_id. Eso rompe dos cosas:
  - las estadisticas por operador quedan PARTIDAS entre filas;
  - `operator_status` (prender/apagar) matchea por NOMBRE, asi que si dos ids de la misma
    persona resuelven a nombres distintos, apagarla NO la apaga entera.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

# FORMATO 1: el conocido, con asteriscos.  "*Maria Jose:* buenas..."
_NAME_RE = re.compile(r"^\*([^:*]{2,40}):\*")

# FORMATO 2: sin asteriscos, el nombre en su propia linea.  "Santiago Angulo:\nPara una..."
# Descubierto el 2026-08-07: rescata 3 de los 4 operadores que estaban sin nombre
# (Santiago Angulo 39 msgs, Josue Escudero 2, MODOSORTI 706).
# DELIBERADAMENTE ANGOSTO para no leer cualquier ':' como una firma:
#   - solo letras, espacios y puntos (nada de digitos, ni URLs, ni horas tipo "12:30");
#   - hasta 3 palabras (un nombre y apellidos, no una oracion);
#   - y el ':' tiene que cerrar la linea (fin de string o salto), que es lo que distingue
#     una firma de un "te comento: ya esta acreditado".
_NAME_PLAIN_RE = re.compile(
    r"^([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:[ .][A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+){0,2}):(?:\n|$)"
)

# Patrones equivalentes para Postgres (regexp_match sobre el body).
_PG_NAME_RE = r"^\*([^:*]{2,40}):\*"
_PG_NAME_PLAIN_RE = r"^([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+([ .][A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+){0,2}):(\n|$)"


def nombre_de_firma(body: str | None) -> str | None:
    """Nombre firmado en el mensaje, probando los DOS formatos. None si no hay firma."""
    texto = (body or "").strip()
    if not texto:
        return None
    for patron in (_NAME_RE, _NAME_PLAIN_RE):
        m = patron.match(texto)
        if m:
            return m.group(1).strip()
    return None


def clave_persona(nombre: str | None) -> str:
    """Clave para decidir si dos nombres son LA MISMA persona.

    Saca tildes, mayusculas y espacios de mas. Sin lo de las tildes, "Anahi" y "Anahí"
    quedaban como dos personas distintas: Anahi tiene 3 user_id con 25.290 mensajes y se
    contaban como 2 con 9.963 (medido el 2026-08-07).

    Conservador a proposito: NO intenta parecidos ni apodos. Fusionar de mas mezclaria
    operadores distintos, que es peor que dejarlos separados — el error se ve al revisar
    una lista, no al mirar una estadistica ya mezclada.
    """
    plano = "".join(
        c for c in unicodedata.normalize("NFD", (nombre or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(plano.split())


def operator_name(messages: list[dict], operator_id) -> str | None:
    """Nombre del operador `operator_id` segun la firma de sus mensajes."""
    if not operator_id:
        return None
    names: Counter = Counter()
    for m in messages:
        if m.get("user_id") != operator_id or not m.get("from_me") or m.get("is_note"):
            continue
        nombre = nombre_de_firma(m.get("body"))
        if nombre:
            names[nombre] += 1
    return names.most_common(1)[0][0] if names else None


def build_operator_map(cur, account: str | None = None) -> dict[str, str]:
    """Mapa GLOBAL user_id -> nombre, leyendo la firma de TODOS los mensajes del operador.

    Resuelve operadores que en una conversacion puntual no firmaron pero si lo hicieron en
    otra. Se scopea por cuenta (un user_id pertenece a una cuenta).

    UNIFICA POR PERSONA: los user_id cuyo nombre normaliza igual (`clave_persona`) reciben
    TODOS la misma grafia — la mas frecuente entre ellos. Asi una persona con la cuenta
    recreada deja de aparecer como dos operadores, y apagarla en la configuracion la apaga
    entera.
    """
    where_acc = "AND account = %s" if account else ""
    cur.execute(
        f"""SELECT user_id,
                   coalesce((regexp_match(body, '{_PG_NAME_RE}'))[1],
                            (regexp_match(body, '{_PG_NAME_PLAIN_RE}'))[1]) AS name,
                   count(*) AS n
              FROM messages
             WHERE from_me AND NOT is_note AND user_id IS NOT NULL
               AND (body ~ '{_PG_NAME_RE}' OR body ~ '{_PG_NAME_PLAIN_RE}') {where_acc}
             GROUP BY user_id, name""",
        (account,) if account else None,
    )
    filas = [(str(u), n, int(c)) for u, n, c in cur.fetchall() if n]

    # 1) la grafia dominante de cada user_id
    mejor: dict[str, tuple[str, int]] = {}
    for user_id, nombre, n in filas:
        if user_id not in mejor or n > mejor[user_id][1]:
            mejor[user_id] = (nombre, n)

    # 2) la grafia CANONICA de cada persona (la mas usada entre todos sus user_id)
    votos: dict[str, Counter] = {}
    for user_id, (nombre, n) in mejor.items():
        votos.setdefault(clave_persona(nombre), Counter())[nombre] += n
    canonico = {k: c.most_common(1)[0][0] for k, c in votos.items()}

    return {uid: canonico[clave_persona(nombre)] for uid, (nombre, _) in mejor.items()}
