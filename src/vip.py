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
        verificacion text,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (account, contact_id)
    )""",
    # El indice PARCIAL y no uno comun: la alerta solo pregunta por los ENCENDIDOS, y los
    # apagados no tienen por que ocupar lugar en el arbol.
    # ALTER para tablas ya creadas por una version previa (mismo patron que
    # `conversions._CREATE_STMTS`): la de la copia ya existe y el CREATE no agrega columnas.
    "ALTER TABLE vip_players ADD COLUMN IF NOT EXISTS verificacion text",
    "CREATE INDEX IF NOT EXISTS idx_vip_players_on ON vip_players (account) WHERE es_vip",
)

# `ranking` y no `rank`: `rank` es palabra reservada del SQL (la funcion de ventana) y
# obliga a citar la columna en cada consulta.
_INSERT = """
INSERT INTO vip_players
    (account, contact_id, es_vip, username, player_id, agencia, ranking, motivo,
     confianza, verificacion)
VALUES (%(account)s, %(contact_id)s, %(es_vip)s, %(username)s, %(player_id)s,
        %(agencia)s, %(ranking)s, %(motivo)s, %(confianza)s, %(verificacion)s)
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
    verificacion = EXCLUDED.verificacion,
    updated_at = now()
"""

# --- LA VERIFICACION: ¿la evidencia alcanza para alertar? -------------------
#
# NO ES LO MISMO QUE LA CONFIANZA. `confianza` dice COMO se encontro el vinculo (telefono,
# etiqueta, mencion); `verificacion` dice si eso ALCANZA. Se estaban mezclando, y por eso
# `brysuye` --que se encontro "con etiqueta", o sea confianza alta-- estaba apuntando a
# `Cristhian Oleas`, que lo menciono UNA vez contra las 177 del contacto correcto.
#
# Decision del negocio (2026-08-26): "solo los confirmados estaran activos, los demas en
# stanby hasta que consiga ver quienes son".

CONFIRMADO, PROBABLE, DUDOSO = "confirmado", "probable", "dudoso"

# Cuantas menciones CON etiqueta alcanzan, si nadie compite por el username.
_ETIQUETAS_QUE_CONFIRMAN = 5
_ETIQUETAS_QUE_APOYAN = 2


def clasificar_vinculo(e: dict) -> tuple[str, list[str]]:
    """`(verificacion, pruebas)` a partir de la evidencia junta.

    CONFIRMA una sola de estas, porque cada una es una IDENTIDAD y no una inferencia:
      · el username ES un telefono y coincide exacto con `contacts.number`
      · el username normalizado ES el nombre del contacto (evelynpalacios = Evelyn Palacios)
      · el contacto esta en la cola `Jugadores VIP` del CRM -- lo dice el CRM, no nosotros
      · un operador lo cargo en `extraInfo` bajo la clave `usuario`
      · 5+ menciones CON etiqueta y NADIE compitiendo por ese username

    NO CONFIRMA una mencion suelta, ni muchas etiquetas si otro contacto pelea el username:
    un agente nombra a muchos jugadores y ahi es donde nacen los falsos.
    """
    pr = []
    if e.get("es_telefono_exacto"):
        pr.append("el username es un teléfono y coincide exacto con el contacto")
    if e.get("nombre_es_el_username"):
        pr.append("el username ES el nombre del contacto")
    if e.get("en_cola_vip"):
        pr.append("el contacto está en la cola `Jugadores VIP` del CRM")
    if e.get("en_extrainfo"):
        pr.append("un operador lo cargó en extraInfo como `usuario`")
    etq, domina = e.get("etiquetas") or 0, e.get("domina", True)
    if etq >= _ETIQUETAS_QUE_CONFIRMAN and domina:
        pr.append(f"{etq} menciones con etiqueta, sin otro contacto compitiendo")
    if pr:
        return CONFIRMADO, pr
    if etq >= _ETIQUETAS_QUE_APOYAN and domina:
        return PROBABLE, [f"{etq} menciones con etiqueta, sin competencia"]
    if e.get("nombre_encaja"):
        return PROBABLE, ["el nombre del contacto encaja con el username"]
    faltas = [f"{etq} menciones con etiqueta"]
    if not domina:
        faltas.append("otro contacto compite por el mismo username")
    if not e.get("nombre_encaja"):
        faltas.append("el nombre no encaja con el username")
    return DUDOSO, faltas


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


# Debajo de esta proporcion de vinculos vivos, no es "se borraron algunos contactos": es
# la base equivocada. La mitad es holgado a proposito -- el sintoma que se quiere atrapar
# es el catastrofico (casi ninguno existe), no el ruido normal.
_MINIMO_VINCULOS_VIVOS = 0.5


# LA COLA DEBIL. Un contacto con menos de este porcentaje de las etiquetas del que mas
# tiene no es "otro contacto del mismo jugador": es alguien que lo nombro de paso.
# CASO REAL, encontrado por el negocio leyendo una alerta: `brysuye` salia como "Cristhian
# Oleas" --que es AGENTE-- porque Cristhian lo menciono UNA vez, contra las 177 menciones
# del contacto de "Bryan David Su Ye". `brysuye` es BRYan SU YE.
# El 20% es holgado a proposito: dos lineas de la misma persona quedan parejas (12 y 9 se
# guardan las dos), y lo que corta es el orden de magnitud.
_PISO_DOMINANCIA = 0.2


def dominantes(contactos: list[dict]) -> list[dict]:
    """Descarta los contactos con una cola despreciable de etiquetas."""
    mx = max((c.get("con_etiqueta") or 0) for c in contactos) if contactos else 0
    if mx <= 0:
        return contactos
    return [c for c in contactos if (c.get("con_etiqueta") or 0) >= mx * _PISO_DOMINANCIA]


def contactos_que_existen(cur, claves: list[tuple[str, str]]) -> int:
    """Cuantos de los `(account, contact_id)` existen de verdad en `contacts`."""
    if not claves:
        return 0
    cur.execute(
        "SELECT count(*) FROM contacts c "
        "WHERE (c.account, c.id::text) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))",
        ([k[0] for k in claves], [k[1] for k in claves]))
    return cur.fetchone()[0]


def verificar_vinculos(existen: int, total: int) -> str | None:
    """Revienta si los `contact_id` no apuntan a nadie en ESTA base.

    ES EL GUARD QUE IMPORTA, y reemplaza al de comparar nombres de base. Comparar nombres
    era un proxy malo: la copia local es un RESTORE del dump de EasyPanel, asi que los uuid
    son los mismos y el nombre no -- bloqueaba una carga valida, y no habria atrapado dos
    bases distintas que se llamaran igual. Lo que importa no es de donde salio el archivo,
    es si los uuid apuntan a alguien aca.

    EL MODO DE FALLO QUE EVITA: cargar uuid de otra base da CERO errores en runtime y CERO
    alertas, y "no llega ninguna alerta" es indistinguible de "no hubo VIP hoy".
    """
    if not total:
        return None
    if existen < total * _MINIMO_VINCULOS_VIVOS:
        raise ValueError(
            f"solo {existen} de {total} `contact_id` existen en esta base. "
            f"El archivo se genero contra OTRA base: volve a correr "
            f"scripts/dump_jugadores_vip.py apuntando a la base destino.")
    if existen < total:
        return (f"{total - existen} de {total} contactos ya no existen en esta base "
                f"(borrados del CRM entre el dump y la carga). Se cargan igual.")
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
                # SOLO EL CONFIRMADO ENTRA ENCENDIDO. El resto queda en la tabla APAGADO
                # --en stanby, no borrado-- para poder encenderlo cuando el negocio lo
                # verifique y para que se vea que fue evaluado. Un config viejo sin el
                # campo cae del lado seguro: apagado.
                "es_vip": v.get("verificacion") == CONFIRMADO,
                "verificacion": v.get("verificacion"),
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


def seed_from_config(cur, jugadores: list[dict]) -> tuple[int, int]:
    """Siembra `vip_players` desde la config. Devuelve `(filas, vinculos_vivos)`.

    CORRE EN CADA ARRANQUE del contenedor, igual que `seed_operator_status`. Ahora que el
    JSON se versiona entra a la imagen (`.dockerignore` NO excluye `config/`), asi que un
    commit y un redeploy dejan la lista puesta: se acaban el `scp` y el `docker cp`.

    NO PISA (`ON CONFLICT DO NOTHING`), mismo contrato que `operator_status`: si alguien
    apago un VIP en produccion, ese cambio vive en la BD y un deploy no puede borrarlo. El
    archivo solo llena huecos. Para que el archivo gane hay que pedirlo a mano con
    `scripts/load_jugadores_vip.py --pisar`.

    DEVUELVE LOS VINCULOS VIVOS y no solo las filas: es lo unico que hay que mirar en el
    log del arranque. "0 de 255" significa que los uuid son de otra base y que no va a
    sonar una sola alerta -- sin ese numero, eso se ve igual que un dia tranquilo.
    """
    ensure_table(cur)
    filas = filas_de_config(jugadores)
    if not filas:
        return 0, 0
    vivos = contactos_que_existen(cur, [(f["account"], f["contact_id"]) for f in filas])
    cur.executemany(_SEED, filas)
    # BORRA LO QUE EL CONFIG RETIRO, y solo eso. CASO REAL: `brysuye` seguia alertando
    # como "Cristhian Oleas" despues de subir el arreglo, porque el `ON CONFLICT DO
    # NOTHING` no toca una fila que ya existe y el config ya no la nombraba: quedaba
    # HUERFANA y encendida para siempre.
    # "No pisar" protege lo que alguien apago A MANO. Una fila que el config RETIRO no es
    # la decision de nadie: es un vinculo retractado, y dejarlo prendido manda alertas
    # malas. Se acota a los usernames que el config SIGUE trayendo -- de los que no
    # menciona no opinamos, porque pudieron agregarse a mano.
    cur.execute(
        "DELETE FROM vip_players WHERE username = ANY(%s) "
        "AND (account, contact_id) <> ALL(SELECT unnest(%s::text[]), unnest(%s::text[]))",
        (sorted({f["username"] for f in filas}),
         [f["account"] for f in filas], [f["contact_id"] for f in filas]))
    return len(filas), vivos


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
