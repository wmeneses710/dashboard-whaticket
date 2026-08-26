"""La marca de jugador VIP en NUESTRA base: `vip_players`.

PARA QUE. El negocio quiere alertas especiales cuando un jugador critico escribe: una de
RESUMEN (quien lo atendio, para que, la calificacion, la duracion, el motivo) y otra de
ESPERA LARGA. Las dos arrancan igual --resolver si el contacto que acaba de escribir esta
en la lista--, y eso tiene que costar cero.

POR QUE UNA TABLA Y NO EL ARCHIVO. La primera version dejaba el vinculo en
`config/jugadores_vip.json` con el TELEFONO adentro, y eso pone 108 numeros reales en un
repo. Dado vuelta: la marca vive en la base --que YA tiene esos telefonos en `contacts`,
asi que no expone nada nuevo-- y el archivo se queda con la REFERENCIA. Un `contact_id` es
un uuid; fuera de esta base no dice nada de nadie.

POR QUE UNA TABLA PROPIA Y NO UNA COLUMNA EN `contacts`. `contacts` es del ETL: este repo
no le escribe ni una fila. Agregarle una columna es pedirle a OTRO proyecto que la
respete, y el dia que su upsert reescriba la fila la marca se va sin que nadie se entere.
`vip_players` es nuestra, igual que `player_conversions` y `conversation_scores`.

POR QUE UN BOOLEANO SI ESTAR EN LA TABLA YA ES SER VIP. Porque apagar no es borrar. Un
jugador que deja de ser critico, o un vinculo dudoso que no queremos alertar todavia, tiene
que quedar en `false` CONSERVANDO la referencia: si se borrara la fila, el proximo dump lo
vuelve a meter y la decision se pierde en silencio. Es la misma razon por la que
`config/operadores.json` lista los APAGADOS y no los activos.

DE DONDE SALE EL VINCULO. `scripts/dump_jugadores_vip.py`, y la confianza que trae importa:
`alta` es el telefono exacto (`0981601125` -> `593981601125`) o el username precedido de
una etiqueta (`Estimado X`, `Perfil X`); `media` es una mencion suelta en un solo contacto;
`baja` es una mencion en varios y casi seguro es una palabra comun --`quezada` cae en 20
contactos y `medardo` en 10 porque son apellidos--. `baja` NO entra a la tabla.
"""
from __future__ import annotations

# Idempotente + self-healing, como `conversions.ensure_table`: el loader la asegura al
# correr y `db/vip_players_schema.sql` queda como referencia, no como paso manual.
_CREATE_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS vip_players (
        account     text        NOT NULL,
        contact_id  text        NOT NULL,
        es_vip      boolean     NOT NULL DEFAULT true,
        username    text,
        player_id   text,
        agencia     text,
        ranking     integer,
        motivo      text,
        confianza   text,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (account, contact_id)
    )""",
    # El indice PARCIAL y no uno comun: la alerta solo pregunta por los ENCENDIDOS, y los
    # apagados no tienen por que ocupar lugar en el arbol.
    "CREATE INDEX IF NOT EXISTS idx_vip_players_on ON vip_players (account) WHERE es_vip",
)

# `ranking` y no `rank`: `rank` es palabra reservada del SQL (la funcion de ventana) y
# obliga a citar la columna en cada consulta.
_INSERT = """
INSERT INTO vip_players
    (account, contact_id, es_vip, username, player_id, agencia, ranking, motivo, confianza)
VALUES (%(account)s, %(contact_id)s, %(es_vip)s, %(username)s, %(player_id)s,
        %(agencia)s, %(ranking)s, %(motivo)s, %(confianza)s)
"""
# SEED: solo llena huecos. Respeta lo que alguien haya apagado a mano en produccion, que
# es justo lo que no podemos ver desde afuera.
_SEED = _INSERT + "ON CONFLICT (account, contact_id) DO NOTHING"
# PISAR: el archivo gana. Para restaurar un estado conocido o empujar un cambio revisado.
_PISAR = _INSERT + """
ON CONFLICT (account, contact_id) DO UPDATE SET
    es_vip     = EXCLUDED.es_vip,
    username   = EXCLUDED.username,
    player_id  = EXCLUDED.player_id,
    agencia    = EXCLUDED.agencia,
    ranking    = EXCLUDED.ranking,
    motivo     = EXCLUDED.motivo,
    confianza  = EXCLUDED.confianza,
    updated_at = now()
"""

# LA CONFIANZA QUE ENTRA A LA TABLA. `baja` NO entra, y no es por prolijidad: `baja`
# significa que el username cae en MUCHOS contactos (`quezada` en 20, `medardo` en 10,
# porque son apellidos). Los 15 jugadores `baja` generaban **64 filas**, o sea 49
# referencias a gente que no tiene nada que ver, y encender a `quezada` habria encendido
# 20 contactos ajenos de una. La tabla es para vinculos RESUELTOS; los `baja` se quedan en
# `config/jugadores_vip.json`, que es donde se ve que fueron buscados y no se pudo.
CONFIANZA_QUE_ENTRA = ("alta", "media")


def base_de(dsn: str | None) -> str | None:
    """El nombre de la base de un DSN. `postgresql://u:p@h:5432/whaticket_copia` -> `whaticket_copia`."""
    if not dsn:
        return None
    resto = dsn.rsplit("/", 1)[-1] if "/" in dsn else ""
    nombre = resto.split("?", 1)[0].strip()
    return nombre or None


def verificar_origen(doc: dict, base_destino: str | None) -> str | None:
    """Revienta si el config se genero contra OTRA base. Devuelve un aviso si no se sabe.

    POR QUE. `contact_id` es un uuid de UNA base. El flujo natural es generar el JSON
    contra la copia --que es donde se investiga-- y cargarlo contra produccion. Si los uuid
    no fueran los mismos, los 255 vinculos apuntarian a nadie: cero errores, cero alertas,
    y "no llega ninguna alerta" es indistinguible de "no hubo VIP hoy". Es el fallo mas
    caro que tiene este despliegue, y el mas facil de no ver.
    """
    origen = doc.get("origen_bd")
    if origen is None:
        return ("el config no dice contra que base se genero (es anterior a este guard): "
                "verificar a mano que los contact_id sean de la base destino")
    if base_destino and origen != base_destino:
        raise ValueError(
            f"el config se genero contra `{origen}` y se esta cargando en `{base_destino}`. "
            f"Los `contact_id` son uuid de UNA base: volve a correr "
            f"scripts/dump_jugadores_vip.py apuntando a `{base_destino}`.")
    return None


def ensure_table(cur) -> None:
    """Crea `vip_players` y su indice si faltan (idempotente)."""
    for stmt in _CREATE_STMTS:
        cur.execute(stmt)


def filas_de_config(jugadores: list[dict]) -> list[dict]:
    """Las fichas de `config/jugadores_vip.json` -> filas de `vip_players`.

    UNA FILA POR CONTACTO, no por jugador: el mismo numero vive en `datos` y en `sistemas`
    con filas de `contacts` distintas (son 356 numeros), y la alerta corre por cuenta.
    Un jugador SIN contacto no genera fila: no hay a quien marcar, y meter una fila vacia
    solo ensucia el join.
    """
    filas = []
    for j in jugadores:
        v = j.get("vinculo") or {}
        confianza = v.get("confianza")
        if confianza not in CONFIANZA_QUE_ENTRA:
            continue
        for c in v.get("contactos") or []:
            filas.append({
                "account": c["account"],
                "contact_id": c["contact_id"],
                "es_vip": True,          # entra encendido; apagar es a mano
                "username": j.get("username"),
                "player_id": j.get("player_id"),
                "agencia": j.get("agencia"),
                "ranking": j.get("rank"),
                "motivo": j.get("motivo"),
                "confianza": confianza,
            })
    return filas


def apply_config(cur, jugadores: list[dict], *, pisar: bool = False) -> int:
    """Empuja las fichas a la base. Devuelve las filas escritas.

    REVIENTA SI DOS JUGADORES CAEN EN EL MISMO CONTACTO, y no es celo: asi se encontro el
    bug de los grupos. El primer load mandaba 277 filas y la tabla quedaba con 263 --
    `executemany` con `ON CONFLICT DO UPDATE` pisaba los choques en silencio--, y los 14
    perdidos eran doce usernames vinculados al grupo `Atencion al Cliente`. Un choque
    significa que el vinculo esta mal; hay que verlo, no resolverlo pisando.
    """
    ensure_table(cur)
    filas = filas_de_config(jugadores)
    if not filas:
        return 0
    vistas: dict[tuple[str, str], str] = {}
    for f in filas:
        clave = (f["account"], f["contact_id"])
        if clave in vistas:
            raise ValueError(
                f"dos jugadores en el mismo contacto {clave[1]} ({clave[0]}): "
                f"{vistas[clave]} y {f['username']}. Suele ser un GRUPO de WhatsApp "
                f"colado en el vinculo; revisar scripts/dump_jugadores_vip.py")
        vistas[clave] = f["username"]
    cur.executemany(_PISAR if pisar else _SEED, filas)
    return len(filas)


def filas_huerfanas(cur, jugadores: list[dict]) -> list[tuple[str, str]]:
    """Las filas de la tabla que el archivo YA NO tiene: `[(account, contact_id), ...]`.

    Hacen falta porque un vinculo puede DEJAR de ser valido, y una fila vieja no se
    corrige sola. Paso de verdad: la primera corrida vinculo tres GRUPOS de WhatsApp y,
    al arreglar el dump, esas filas quedaron en la tabla alertando sobre un grupo interno.
    Un upsert nunca las habria tocado.
    """
    vivas = {(f["account"], f["contact_id"]) for f in filas_de_config(jugadores)}
    cur.execute("SELECT account, contact_id FROM vip_players")
    return sorted(set(cur.fetchall()) - vivas)


def podar(cur, jugadores: list[dict]) -> int:
    """Borra las filas que el archivo ya no tiene. Devuelve cuantas.

    VA APARTE DE `--pisar` a proposito: pisar corrige lo que el archivo conoce, podar
    BORRA lo que no. Alguien puede haber agregado un VIP a mano en produccion --que es
    justo el caso que `es_vip` existe para soportar-- y no puede desaparecer porque el
    dump de hoy no lo trajo. Se pide explicito.
    """
    huerfanas = filas_huerfanas(cur, jugadores)
    if not huerfanas:
        return 0
    cur.execute("DELETE FROM vip_players WHERE (account, contact_id) IN "
                "(SELECT unnest(%s::text[]), unnest(%s::text[]))",
                ([h[0] for h in huerfanas], [h[1] for h in huerfanas]))
    return len(huerfanas)


def contactos_vip(cur, account: str) -> dict[str, dict]:
    """`{contact_id: ficha}` de los VIP ENCENDIDOS de una cuenta.

    Se lee entero y se resuelve en memoria: son 320 contactos, y la alerta tiene que
    decidir por mensaje sin pegarle a la base cada vez.
    """
    cur.execute(
        "SELECT contact_id, username, player_id, agencia, ranking, motivo "
        "FROM vip_players WHERE account = %s AND es_vip", (account,))
    return {r[0]: {"username": r[1], "player_id": r[2], "agencia": r[3],
                   "ranking": r[4], "motivo": r[5]} for r in cur.fetchall()}
