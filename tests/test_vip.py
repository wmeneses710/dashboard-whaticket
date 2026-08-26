"""Tests de src/vip.py: la marca de jugador VIP en NUESTRA base.

POR QUE LA TABLA Y NO EL ARCHIVO. La primera version dejaba el vinculo en
`config/jugadores_vip.json` con el TELEFONO adentro, y eso pone 108 numeros reales en un
repo. El negocio pidio darlo vuelta: la marca vive en la BD --que ya tiene esos telefonos
en `contacts`, asi que no expone nada nuevo-- y el archivo se queda solo con la
REFERENCIA. Un `contact_id` es un uuid: fuera de esta base no dice nada de nadie.

DONDE VA LA TABLA, y es la decision de diseño. `contacts` es del ETL: este repo NO le
escribe ni una fila (verificado, no hay un solo INSERT/UPDATE contra ella). Agregarle una
columna es pedirle a otro proyecto que la respete, y el dia que su upsert reescriba la
fila la marca se va sin que nadie se entere. `vip_players` es NUESTRA, igual que
`player_conversions`, `conversation_scores` y `conversation_sessions`.

POR QUE UN BOOLEANO SI ESTAR EN LA TABLA YA ES SER VIP. Porque apagar no es lo mismo que
borrar: un jugador que deja de ser critico, o un vinculo dudoso que no queremos alertar,
tiene que poder quedar en `false` CONSERVANDO la referencia. Si se borrara la fila, el
proximo dump lo vuelve a meter y la decision se pierde en silencio.
"""
from src import vip


class _FakeCursor:
    """NO ejecuta SQL: guarda lo que le mandan. Igual que en tests/test_conversions.py."""

    def __init__(self, rows=None):
        self.sql = []
        self.params = []
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(params)

    def executemany(self, sql, seq):
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(list(seq))
        self.rowcount = len(list(seq))

    def fetchall(self):
        return self._rows


# --- el esquema -------------------------------------------------------------

def test_ensure_table_crea_la_tabla_y_es_idempotente():
    """Self-healing como `conversions.ensure_table`: el loader la asegura al correr y
    `db/vip_players_schema.sql` queda como referencia, no como paso manual."""
    cur = _FakeCursor()
    vip.ensure_table(cur)
    junto = " ".join(cur.sql)
    assert "CREATE TABLE IF NOT EXISTS vip_players" in junto
    assert "PRIMARY KEY (account, contact_id)" in junto
    assert "CREATE INDEX IF NOT EXISTS" in junto
    n = len(cur.sql)
    vip.ensure_table(cur)
    assert len(cur.sql) == 2 * n, "correrla dos veces no puede fallar"


def test_la_tabla_NO_guarda_el_telefono():
    """El telefono ya vive en `contacts.number`: repetirlo acá no agrega nada y multiplica
    el lugar donde hay que ir a borrarlo. La fila se ata por `contact_id`."""
    cur = _FakeCursor()
    vip.ensure_table(cur)
    junto = " ".join(cur.sql).lower()
    assert "numero" not in junto and "number" not in junto and "telefono" not in junto


# --- de la config a las filas -----------------------------------------------

def _jug(username="quirozsabando", confianza="alta", contactos=None, **kw):
    d = {"username": username, "player_id": "49846", "agencia": "ModoSorti", "rank": 1,
         "motivo": "GGRx5", "kyc": True, "ggr_casa": 95.08, "turnover": 722.29,
         "depositos": 110.0, "retiros": 40.0,
         "vinculo": {"confianza": confianza, "metodo": "telefono", "mensajes": 12,
                     "ultimo_mensaje": "2026-08-25",
                     "contactos": contactos if contactos is not None
                     else [{"contact_id": "c1", "account": "sistemas"}]}}
    d.update(kw)
    return d


def test_un_jugador_con_contacto_en_DOS_cuentas_da_DOS_filas():
    """El mismo numero vive en `datos` y en `sistemas` con filas de `contacts` distintas:
    son 356 numeros. La alerta corre por cuenta, asi que hacen falta las dos."""
    filas = vip.filas_de_config([_jug(contactos=[
        {"contact_id": "a1", "account": "datos"},
        {"contact_id": "b2", "account": "sistemas"}])])
    assert {(f["account"], f["contact_id"]) for f in filas} == {("datos", "a1"), ("sistemas", "b2")}


def test_el_jugador_SIN_contacto_no_genera_fila():
    """68 de los 334 no dejaron rastro. Sin `contact_id` no hay a quien marcar, y meter una
    fila vacia solo ensucia el join de la alerta."""
    assert vip.filas_de_config([_jug(confianza="ninguna", contactos=[])]) == []


def test_el_vinculo_BAJA_no_entra_a_la_tabla():
    """LO MEDIDO CAMBIO EL DISEÑO. Primero los `baja` entraban apagados, "para que se vea
    que fueron evaluados". El dry-run mostro el problema: 15 jugadores `baja` generaban
    **64 filas**, porque `baja` significa justamente que el username cae en MUCHOS
    contactos --`quezada` en 20, `medardo` en 10--. Escribirlos mete 49 referencias a
    gente que no tiene nada que ver, y el dia que alguien encienda a `quezada` enciende 20
    contactos ajenos de una. La tabla es para vinculos RESUELTOS; los `baja` quedan en el
    JSON, que es donde se ve que fueron buscados."""
    filas = vip.filas_de_config([_jug("a", "alta"), _jug("b", "media"),
                                 _jug("c", "baja", contactos=[
                                     {"contact_id": "x1", "account": "datos"},
                                     {"contact_id": "x2", "account": "datos"}])])
    assert {f["username"] for f in filas} == {"a", "b"}
    assert all(f["es_vip"] for f in filas), "lo que entra, entra encendido"


def test_el_booleano_sirve_para_APAGAR_a_mano_no_para_la_confianza():
    """`es_vip` no codifica la confianza --eso ya lo filtro la entrada--: existe para que
    el negocio pueda apagar un VIP sin perder su referencia."""
    f = vip.filas_de_config([_jug()])[0]
    assert f["es_vip"] is True and f["confianza"] == "alta"


def test_la_fila_lleva_la_referencia_que_la_alerta_muestra():
    """La alerta de resumen nombra al jugador; sin esto habria que volver al CSV."""
    f = vip.filas_de_config([_jug()])[0]
    assert f["username"] == "quirozsabando" and f["player_id"] == "49846"
    assert f["agencia"] == "ModoSorti" and f["ranking"] == 1 and f["motivo"] == "GGRx5"
    assert f["confianza"] == "alta"


# --- empujar a la base ------------------------------------------------------

def test_seed_no_pisa_lo_que_ya_hay_y_pisar_si():
    """Mismos dos modos que `scripts/load_operadores.py`, y por el mismo motivo: alguien
    puede haber apagado un VIP a mano en produccion y el dump no puede borrarlo sin querer."""
    cur = _FakeCursor()
    vip.apply_config(cur, [_jug()], pisar=False)
    assert "ON CONFLICT (account, contact_id) DO NOTHING" in " ".join(cur.sql)
    cur = _FakeCursor()
    vip.apply_config(cur, [_jug()], pisar=True)
    junto = " ".join(cur.sql)
    assert "DO UPDATE SET" in junto and "es_vip" in junto


def test_apply_config_asegura_la_tabla_antes_de_escribir():
    cur = _FakeCursor()
    vip.apply_config(cur, [_jug()], pisar=False)
    assert "CREATE TABLE IF NOT EXISTS vip_players" in cur.sql[0]


def test_sin_jugadores_no_escribe_nada():
    cur = _FakeCursor()
    assert vip.apply_config(cur, [], pisar=True) == 0
    assert not any("INSERT INTO vip_players" in s for s in cur.sql)


# --- leer, que es lo que hace la alerta -------------------------------------

def test_contactos_vip_devuelve_solo_los_encendidos():
    """El webhook resuelve por `contact_id`: un dict en memoria y cero consultas por
    mensaje. Los apagados no viajan."""
    cur = _FakeCursor(rows=[("c1", "quirozsabando", "49846", "ModoSorti", 1, "GGRx5")])
    out = vip.contactos_vip(cur, "sistemas")
    assert "WHERE account = %s AND es_vip" in " ".join(cur.sql)
    assert out["c1"]["username"] == "quirozsabando"
    assert out["c1"]["agencia"] == "ModoSorti"


# --- LA PERDIDA SILENCIOSA, que es como se encontro el bug del grupo ---------

def test_dos_jugadores_en_el_MISMO_contacto_revientan_en_vez_de_pisarse():
    """CASO REAL, y por esto existe el test. El primer load mandaba 277 filas y la tabla
    quedaba con 263: `executemany` + `ON CONFLICT DO UPDATE` pisaba en silencio los
    choques de `(account, contact_id)`. Los 14 perdidos destaparon el bug de verdad --tres
    GRUPOS de WhatsApp (`Atención al Cliente`, `Reclamos`, `AVISOS ATC`) donde el personal
    habla SOBRE los jugadores, y doce usernames distintos se vinculaban al mismo contacto--.

    Si dos jugadores caen en el mismo contacto, algo esta mal en el vinculo: hay que verlo,
    no resolverlo pisando."""
    import pytest
    cur = _FakeCursor()
    con_choque = [_jug("a", contactos=[{"contact_id": "c1", "account": "sistemas"}]),
                  _jug("b", contactos=[{"contact_id": "c1", "account": "sistemas"}])]
    with pytest.raises(ValueError, match="c1"):
        vip.apply_config(cur, con_choque, pisar=True)


def test_el_mismo_jugador_en_dos_cuentas_NO_es_un_choque():
    """`(account, contact_id)` es la clave: el mismo contacto en `datos` y en `sistemas`
    son dos filas legitimas."""
    cur = _FakeCursor()
    assert vip.apply_config(cur, [_jug(contactos=[
        {"contact_id": "c1", "account": "datos"},
        {"contact_id": "c1", "account": "sistemas"}])], pisar=True) == 2


# --- las huerfanas: un vinculo que dejo de ser valido ------------------------

def test_las_filas_que_el_archivo_ya_no_tiene_se_detectan():
    """CASO REAL. Al arreglar el dump para excluir los grupos de WhatsApp, las filas que
    apuntaban a esos grupos QUEDARON en la tabla: un upsert nunca las toca. La tabla
    seguia alertando sobre `Atención al Cliente`."""
    cur = _FakeCursor(rows=[("sistemas", "c1"), ("sistemas", "grupo_viejo"),
                            ("datos", "otro_viejo")])
    assert vip.filas_huerfanas(cur, [_jug()]) == [("datos", "otro_viejo"),
                                                  ("sistemas", "grupo_viejo")]


def test_podar_va_APARTE_de_pisar():
    """Pisar corrige lo que el archivo conoce; podar BORRA lo que no. Alguien puede haber
    agregado un VIP a mano en produccion --el caso que `es_vip` existe para soportar-- y
    no puede desaparecer porque el dump de hoy no lo trajo. Se pide explicito."""
    cur = _FakeCursor(rows=[("sistemas", "c1"), ("sistemas", "viejo")])
    assert vip.podar(cur, [_jug()]) == 1
    assert "DELETE FROM vip_players" in " ".join(cur.sql)
    limpio = _FakeCursor(rows=[("sistemas", "c1")])
    assert vip.podar(limpio, [_jug()]) == 0
    assert not any("DELETE" in s for s in limpio.sql), "sin huerfanas no se borra nada"


# --- EL GUARD DEL DESPLIEGUE ------------------------------------------------
#
# `contact_id` es un uuid de UNA base. El JSON se genera contra la copia y se carga contra
# produccion: si los uuid no fueran los mismos, los 255 vinculos apuntarian a nadie y la
# alerta callaria para siempre sin un solo error. No es hipotetico -- es el orden natural
# de trabajo, y el sintoma seria "no llega ninguna alerta", que es indistinguible de "no
# hubo VIP hoy".

def test_el_dump_estampa_contra_QUE_BASE_se_genero():
    assert vip.base_de("postgresql://u:p@host:5432/whaticket_copia") == "whaticket_copia"
    assert vip.base_de("postgresql://u:p@h/prod?sslmode=require") == "prod"
    assert vip.base_de("") is None


def test_cargar_un_config_de_OTRA_base_se_planta():
    """Falla ruidosa y no silenciosa: el modo de fallo caro es cargar uuid de la copia en
    produccion y que todo 'ande' sin alertar nunca."""
    import pytest
    with pytest.raises(ValueError, match="whaticket_copia"):
        vip.verificar_origen({"origen_bd": "whaticket_copia"}, "whaticket_prod")


def test_el_mismo_origen_pasa_sin_chistar():
    vip.verificar_origen({"origen_bd": "whaticket_prod"}, "whaticket_prod")


def test_un_config_VIEJO_sin_estampa_no_bloquea_pero_avisa():
    """Los generados antes de este guard no tienen el campo. No se rompe el flujo: se
    devuelve el aviso para que el script lo imprima."""
    assert vip.verificar_origen({}, "whaticket_prod") is not None
