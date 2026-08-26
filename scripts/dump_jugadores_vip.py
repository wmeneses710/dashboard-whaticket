"""Genera config/jugadores_vip.json: los jugadores VIP y su vinculo con la BD.

PARA QUE. El negocio quiere alertas especiales cuando un jugador critico escribe. Una
alerta necesita saber A QUIEN vigilar, y el reporte de negocio identifica al jugador por
`username`, que es un dato del CASINO -- no existe en la base del CRM. Este script tiende
ese puente UNA vez y lo deja escrito, para que la alerta en caliente resuelva por
`contact_id` y no vuelva a adivinar.

COMO SE TIENDE EL PUENTE, y son dos caminos con confianza MUY distinta:

  1. EL TELEFONO (`confianza: alta`). 140 de los 334 usernames SON un numero de telefono.
     `0981601125` -> `593981601125`, que es el formato de `contacts.number`. Es una
     igualdad exacta, no un heuristico. VERIFICADO con un control por los ultimos 9
     digitos: da el MISMO conjunto, asi que el prefijo no esta inventando ni perdiendo
     nada.

  2. EL USERNAME ESCRITO EN EL MENSAJE (`alta` o `media`). Los otros 194 solo aparecen
     como texto ("Estimado deyberjb7 llene el siguiente formulario", "Perfil vinic88").
     Buscarlos a secas NO alcanza y el dato lo demuestra: `rojas` matchea en 78 contactos
     distintos porque es un apellido, `quezada` en 20, `medardo` en 10.

     EL DISCRIMINADOR ES LA ETIQUETA, la misma leccion que src/censura.py: un username
     precedido de `estimado` / `perfil` / `usuario` / `cuenta` ES un username; la misma
     palabra suelta puede ser cualquier cosa. Con la etiqueta, `rojas` se fija en UN
     contacto de los 78.

LO QUE ESTE ARCHIVO NO ES. No es una lista de a quien alertar y ya: los `media` y los
`ninguna` estan a proposito para que se vean. Un `media` es "aparece una vez y en un solo
contacto, pero nadie lo llamo usuario"; alertarlo puede ser correcto o puede ser ruido, y
esa decision es del negocio.

QUE SALE Y QUE NO. El archivo SE VERSIONA, asi que lleva lo minimo:

  ENTRA  `username`, `player_id`, `agencia`, `rank`, `motivo` y la lista de `contact_id`.
         Son las llaves del negocio y las que los dos mensajes imprimen.
  NO ENTRA  el telefono (`contacts.number`) ni el dato financiero (`ggr_casa`, `turnover`,
         `depositos`, `retiros`, `kyc`). Lo financiero venia del reporte y no lo lee NADIE
         --ni el loader ni los mensajes--, pero es lo mas sensible que habia: cuanto
         deposita, cuanto retira y cuanto pierde una persona identificable, en un repo,
         para siempre. Lo que no se usa no se guarda.

`contact_id` es un uuid: fuera de esta base no dice nada de nadie. Empujarlo a la tabla es
`scripts/load_jugadores_vip.py`.

OJO: 140 de los 334 `username` SON un numero de telefono, porque asi los identifica el
casino. Viene del reporte de origen y no se puede quitar sin perder la llave con la que el
negocio nombra a sus jugadores -- es el mismo dato que ya circula en el CSV.

Uso:  .venv/bin/python scripts/dump_jugadores_vip.py <reporte.csv> [salida.json]
"""
from __future__ import annotations

import csv
import json
import unicodedata
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.vip import (  # noqa: E402
    CONFIRMADO, DUDOSO, PROBABLE, base_de, clasificar_vinculo, dominantes)

# El contexto que convierte una palabra en un USERNAME. Sale del corpus: es lo que el
# operador escribe alrededor de una cuenta.
_CTX_USUARIO = r"(?:estimad[oa]|perfil|usuario|user|cuenta|jugador|agencia|ag)\M[^\w]{0,4}"
_ES_TELEFONO = re.compile(r"\d{6,}")

# De mejor a peor, para poder ordenar y comparar.
DUDOSO_ORDEN = [CONFIRMADO, PROBABLE, DUDOSO]

# EL GRUPO DE WHATSAPP NO ES UNA PERSONA, y era el falso positivo mas caro. `Atencion al
# Cliente` (36.862 mensajes) y `Reclamos` son grupos INTERNOS donde el personal habla
# SOBRE los jugadores: ahi aparecen doce usernames distintos, cada uno con su etiqueta, y
# los doce se vinculaban al mismo contacto. Alertar ahi es al reves de lo que se pide --el
# jugador no escribio, hablaron de el--.
#
# EL LARGO DEL NUMERO Y NO `is_group`: de los tres grupos que aparecieron, `is_group`
# llegaba en false en DOS. `length(number) > 15` es la regla que el repo ya usa para esto
# (ver el comentario de _rows_as_dicts sobre por que la censura conserva el largo).


def cargar_reporte(ruta: str) -> list[dict]:
    with open(ruta, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def _nombre_es_el_username(username: str, nombre: str | None) -> bool:
    """`evelynpalacios` y "Evelyn Palacios" normalizados son EL MISMO string.

    No es parecido: es el mismo nombre escrito de dos formas. El piso de 8 caracteres deja
    fuera los cortos, donde una coincidencia puede ser casualidad.
    """
    letras = re.sub(r"\d", "", _norm(username))
    return len(letras) >= 8 and letras == _norm(nombre)


def _nombre_encaja(username: str, nombre: str | None) -> bool:
    """El username DERIVA del nombre (`brysuye` <- BRYan SU YE). APOYA, no confirma.

    Se probo como criterio unico y es demasiado ruidoso: marcaba mal a `rafael12` /
    "Rafa Ramirez" y a `toni01` / "Anthony Loor". Por eso nunca alcanza sola.
    """
    letras, n = re.sub(r"\d", "", _norm(username)), _norm(nombre)
    if len(letras) < 4 or not n:
        return False
    if letras in n or n in letras:
        return True
    resto, cubierto = letras, 0
    for palabra in (_norm(p) for p in (nombre or "").split()):
        if len(palabra) < 2:
            continue
        for k in range(len(palabra), 1, -1):
            if resto.startswith(palabra[:k]):
                resto, cubierto = resto[k:], cubierto + k
                break
    return cubierto >= len(letras) * 0.7


def _telefono_de(username: str) -> str | None:
    """`0981601125` -> `593981701125`. El formato de `contacts.number` es 593 + 9 digitos."""
    if not _ES_TELEFONO.fullmatch(username):
        return None
    return "593" + username.lstrip("0")


def vincular(cur, filas: list[dict]) -> dict[str, dict]:
    """{username: {confianza, metodo, contactos, mensajes, ultimo}}."""
    usuarios = sorted({f["username"] for f in filas})
    cur.execute("CREATE TEMP TABLE vip(u text PRIMARY KEY, tel text)")
    with cur.copy("COPY vip(u, tel) FROM STDIN") as cp:
        for u in usuarios:
            cp.write_row((u, _telefono_de(u)))

    vinculo: dict[str, dict] = {}

    # --- LA EVIDENCIA INDEPENDIENTE, una vez para todos ---------------------
    # La cola `Jugadores VIP` la mantiene el CRM: si un contacto esta ahi, lo dijo el
    # negocio y no nosotros. `extraInfo` con la clave `usuario` lo carga un operador a
    # mano. Son las dos unicas fuentes que no dependen de nuestro matcheo.
    cur.execute("""
        SELECT DISTINCT t.account, t.contact_id::text FROM tickets t
        JOIN queues q ON q.id = t.queue_id WHERE q.name = 'Jugadores VIP'""")
    en_cola = set(cur.fetchall())
    cur.execute("""
        SELECT lower(trim(x->>'value')), rt.account, rt.payload->'contact'->>'id'
        FROM raw_tickets rt, jsonb_array_elements(rt.payload->'contact'->'extraInfo') x
        WHERE jsonb_typeof(rt.payload->'contact'->'extraInfo') = 'array'
          AND lower(trim(x->>'name')) = 'usuario'""")
    en_extra: dict[str, set] = {}
    for valor, acc, cid in cur.fetchall():
        en_extra.setdefault(valor, set()).add((acc, cid))

    # --- 1. EL TELEFONO: igualdad exacta contra contacts.number -------------
    cur.execute("""
        SELECT v.u, c.id::text, c.account, c.number, c.name,
               count(m.id) AS msgs, max(m.created_at)::date AS ultimo
        FROM vip v
        JOIN contacts c ON c.number = v.tel
        LEFT JOIN tickets t ON t.contact_id = c.id
        LEFT JOIN messages m ON m.ticket_id = t.id
        WHERE v.tel IS NOT NULL AND NOT (length(c.number) > 15 OR coalesce(t.is_group, false))
        GROUP BY v.u, c.id, c.account, c.number
        HAVING count(m.id) > 0""")
    for u, cid, acc, numero, nombre, msgs, ultimo in cur.fetchall():
        d = vinculo.setdefault(u, {"confianza": "alta", "metodo": "telefono",
                                   "contactos": [], "mensajes": 0, "ultimo_mensaje": None})
        # `numero` viaja SOLO en memoria, para clasificar: no se serializa (ver abajo).
        d["contactos"].append({"contact_id": cid, "account": acc,
                               "nombre": nombre, "_numero": numero})
        d["mensajes"] += msgs
        if ultimo and (d["ultimo_mensaje"] is None or str(ultimo) > d["ultimo_mensaje"]):
            d["ultimo_mensaje"] = str(ultimo)

    # --- 2. EL USERNAME EN EL TEXTO ----------------------------------------
    # Una sola pasada por los mensajes con una alternacion word-bounded, y recien
    # despues se reparte por username: 334 regex sobre 3,2M de filas no termina nunca.
    solo_texto = [u for u in usuarios if _telefono_de(u) is None]
    if solo_texto:
        alternacion = r"\m(" + "|".join(re.escape(u) for u in solo_texto) + r")\M"
        cur.execute("""
            CREATE TEMP TABLE golpe AS
            SELECT m.id, m.ticket_id, m.body FROM messages m
            WHERE m.body IS NOT NULL AND m.body ~* %s""", (alternacion,))
        cur.execute(f"""
            SELECT v.u, c.id::text, c.account, c.number, c.name,
                   count(*) AS msgs,
                   count(*) FILTER (WHERE g.body ~* ('{_CTX_USUARIO}' || v.u || '\\M'))
                       AS con_etiqueta,
                   max(m.created_at)::date AS ultimo
            FROM golpe g
            JOIN vip v ON g.body ~* ('\\m' || v.u || '\\M')
            JOIN messages m ON m.id = g.id
            JOIN tickets t ON t.id = g.ticket_id
            JOIN contacts c ON c.id = t.contact_id
            WHERE v.tel IS NULL AND NOT (length(c.number) > 15 OR coalesce(t.is_group, false))
            GROUP BY v.u, c.id, c.account, c.number""")
        por_usuario: dict[str, list] = {}
        for u, cid, acc, numero, nombre, msgs, con_et, ultimo in cur.fetchall():
            por_usuario.setdefault(u, []).append(
                {"contact_id": cid, "account": acc, "numero": numero, "nombre": nombre,
                 "mensajes": msgs, "con_etiqueta": con_et, "ultimo": str(ultimo) if ultimo else None})
        for u, cts in por_usuario.items():
            # LA COLA DEBIL SE VA ANTES DE ELEGIR. Ver src/vip.dominantes: un contacto
            # que menciono al jugador UNA vez contra otro que lo menciono 177 no es un
            # segundo contacto suyo, es alguien que lo nombro de paso.
            etiquetados = dominantes([c for c in cts if c["con_etiqueta"] > 0])
            # LA ETIQUETA MANDA: si alguien lo llamo "usuario", esos contactos son los
            # buenos y el resto es la palabra suelta. `rojas` pasa de 78 contactos a 1.
            elegidos = etiquetados or cts
            if etiquetados:
                confianza, metodo = "alta", "username_con_etiqueta"
            elif len(cts) == 1:
                confianza, metodo = "media", "username_mencionado"
            else:
                confianza, metodo = "baja", "username_ambiguo"
            vinculo[u] = {
                "confianza": confianza, "metodo": metodo,
                # EL `numero` NO SE SERIALIZA. Hace falta DENTRO del dump para clasificar
                # (comparar el username-telefono contra `contacts.number`), pero el archivo
                # se versiona: el telefono no vuelve al repo por la puerta de atras.
                "contactos": [{k: c[k] for k in ("contact_id", "account", "nombre")}
                              for c in elegidos],
                "mensajes": sum(c["mensajes"] for c in elegidos),
                "ultimo_mensaje": max((c["ultimo"] for c in elegidos if c["ultimo"]), default=None),
                "contactos_descartados": len(cts) - len(elegidos),
            }
    # --- LA VERIFICACION: ¿la evidencia alcanza para ALERTAR? ---------------
    # Va al final y sobre las dos vias por igual: el telefono y el texto llegan aca con la
    # misma pregunta. Ver src/vip.clasificar_vinculo para que confirma y que no.
    for u, d in vinculo.items():
        etiquetas = {(c["account"], c["contact_id"]): c.get("con_etiqueta") or 0
                     for c in por_usuario.get(u, [])}
        mx = max(etiquetas.values(), default=0)
        peor = DUDOSO_ORDEN[0]
        for c in d["contactos"]:
            clave = (c["account"], c["contact_id"])
            etq = etiquetas.get(clave, 0)
            otros = [v for k, v in etiquetas.items() if k != clave]
            ev = {
                "es_telefono_exacto": (_telefono_de(u) is not None
                                       and c.get("_numero") == _telefono_de(u)),
                "nombre_es_el_username": _nombre_es_el_username(u, c.get("nombre")),
                "en_cola_vip": clave in en_cola,
                "en_extrainfo": clave in en_extra.get(u.lower(), set()),
                "etiquetas": etq,
                "domina": not otros or (etq and etq >= max(otros) * 3),
                "nombre_encaja": _nombre_encaja(u, c.get("nombre")),
            }
            ver, pruebas = clasificar_vinculo(ev)
            c["verificacion"], c["pruebas"] = ver, pruebas
            c.pop("_numero", None)
            if DUDOSO_ORDEN.index(ver) > DUDOSO_ORDEN.index(peor):
                peor = ver
        # EL JUGADOR SE QUEDA CON LO MEJOR DE SUS CONTACTOS: si uno esta confirmado, el
        # jugador se alerta. Los contactos flojos igual viajan con su propia marca.
        mejor = min((c["verificacion"] for c in d["contactos"]),
                    key=DUDOSO_ORDEN.index, default=DUDOSO)
        d["verificacion"] = mejor
        d["pruebas"] = next((c["pruebas"] for c in d["contactos"]
                             if c["verificacion"] == mejor), [])
    return vinculo


def construir(filas: list[dict], vinculo: dict[str, dict], fuente: str,
              origen_bd: str | None = None) -> dict:
    jugadores = []
    for f in filas:
        u = f["username"]
        v = vinculo.get(u)
        jugadores.append({
            "username": u,
            "player_id": f["player_id"],
            "agencia": f["agencia"],
            "rank": int(f["rank"]) if f["rank"].isdigit() else None,
            "motivo": f["motivo"],
            # EL DATO FINANCIERO NO ENTRA, y el archivo se versiona. `ggr_casa`,
            # `turnover`, `depositos`, `retiros` y `kyc` venian del reporte y no los lee
            # NADIE --ni el loader ni los dos mensajes--, pero son lo mas sensible que
            # habia: cuanto deposita, cuanto retira y cuanto pierde una persona
            # identificable, en un repo, para siempre. Lo que no se usa no se guarda; si
            # algun dia hace falta, esta en el CSV de origen.
            "vinculo": v or {"confianza": "ninguna", "metodo": None, "contactos": [],
                             "mensajes": 0, "ultimo_mensaje": None},
        })
    jugadores.sort(key=lambda j: (j["agencia"], j["rank"] or 9999))
    conf = {}
    for j in jugadores:
        conf[j["vinculo"]["confianza"]] = conf.get(j["vinculo"]["confianza"], 0) + 1
    return {
        "version": 1,
        "criterio": "jugadores VIP / criticos del reporte de negocio, vinculados a contactos del CRM",
        "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generado_por": "scripts/dump_jugadores_vip.py",
        "fuente": os.path.basename(fuente),
        # CONTRA QUE BASE se resolvieron los `contact_id`. Es lo que impide cargar en
        # produccion uuid sacados de la copia (ver src/vip.verificar_origen).
        "origen_bd": origen_bd,
        "nota": ("La alerta resuelve por `contact_id`. `confianza` dice cuanto vale el "
                 "vinculo: `alta` es el telefono exacto o el username con etiqueta "
                 "(`Estimado X`, `Perfil X`); `media` es una mencion suelta en un solo "
                 "contacto; `baja` es una mencion en varios y probablemente sea una "
                 "palabra comun. Editable a mano: corregir un vinculo aca es valido y "
                 "sobrevive hasta el proximo dump."),
        "nota_sin_dato_personal": ("NO lleva telefonos: la marca vive en `vip_players` "
                                   "(nuestra base, que ya los tiene en `contacts`) y aca "
                                   "queda solo la REFERENCIA. Un `contact_id` es un uuid: "
                                   "fuera de esa base no dice nada de nadie. OJO: 140 de "
                                   "los 334 `username` SON un numero de telefono, porque "
                                   "asi los identifica el casino; eso viene del reporte de "
                                   "origen y no lo agrega este archivo."),
        "resumen": {"jugadores": len(jugadores), "por_confianza": conf},
        "jugadores": jugadores,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    fuente = sys.argv[1]
    salida = sys.argv[2] if len(sys.argv) > 2 else "config/jugadores_vip.json"
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("falta DATABASE_URL", file=sys.stderr)
        return 2
    filas = cargar_reporte(fuente)
    with psycopg.connect(dsn) as cn, cn.cursor() as cur:
        vinculo = vincular(cur, filas)
    doc = construir(filas, vinculo, fuente, base_de(dsn))
    with open(salida, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"{salida}: {doc['resumen']['jugadores']} jugadores, {doc['resumen']['por_confianza']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
