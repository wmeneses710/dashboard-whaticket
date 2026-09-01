"""Tests de src/rescore.py: encolar un rescore PARCIAL por lista de UUIDs.

POR QUE POR LISTA. El rescore corre en EasyPanel, donde lo que hay es una consola y una
caja de texto -- no una terminal con un archivo al lado. El negocio lo pidio asi: "debe
aceptar una lista o una linea de uuids con algun separador para que las ponga para el
reescore y asi se encolen".

LOS TRES IDENTIFICADORES Y SU RELACION, medidos sobre la copia (4.705 filas):

    ticket (831)  --1:N-->  sesion (1.064)  --1:N-->  interaccion (4.705)

  * `ticket_id` NUNCA es NULL, asi que es una puerta de entrada valida.
  * La relacion es ESTRICTA hacia arriba: ninguna sesion pertenece a dos tickets
    (`max_tickets_por_sesion = 1`). Por eso se puede resolver un id sin ambiguedad.
  * Hacia abajo NO: un ticket llega a 31 sesiones y 167 interacciones. Marcar un ticket
    puede encolar 167 filas, y eso hay que MOSTRARLO antes de escribir.

Los tres son uuid y viven en columnas distintas, asi que un id se clasifica preguntando.
"""
from src import rescore


class _FakeCursor:
    """Cursor falso. `filas` mapea (columna -> ids que existen en esa columna)."""

    def __init__(self, filas=None, rowcount=0):
        self.filas = filas or {}
        self.sql, self.params, self._rows = [], [], []
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        texto = " ".join(str(sql).split())
        self.sql.append(texto)
        self.params.append(params)
        for col, ids in self.filas.items():
            if f"{col} = ANY" in texto or f"{col}::text = ANY" in texto:
                pedidos = params[0] if params else []
                self._rows = [(i,) for i in pedidos if i in ids]
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# --- parsear lo que alguien pega en una caja de texto -----------------------

def test_acepta_una_LINEA_separada_por_comas():
    ids, basura = rescore.parse_uuids(
        "16847caa-9e5b-5b01-8531-2e576240e820,bfe635f8-ed7d-538d-9c16-76621576eda6")
    assert len(ids) == 2 and basura == []


def test_acepta_CUALQUIER_separador_razonable():
    """Nadie va a normalizar a mano lo que copia de una consulta o de un Excel."""
    crudo = ("16847caa-9e5b-5b01-8531-2e576240e820\n"
             "bfe635f8-ed7d-538d-9c16-76621576eda6 ; "
             "c9ca9077-94df-57dc-986e-be958ed43ca5 | "
             "  ccb8af46-a88a-52da-b915-38421321765d  ,\n")
    ids, basura = rescore.parse_uuids(crudo)
    assert len(ids) == 4 and basura == []


def test_saca_las_COMILLAS_y_los_parentesis_que_deja_un_copy_paste():
    """Copiar de un resultado de psql o de una lista de Python trae adornos."""
    ids, basura = rescore.parse_uuids(
        "['16847caa-9e5b-5b01-8531-2e576240e820', \"bfe635f8-ed7d-538d-9c16-76621576eda6\"]")
    assert len(ids) == 2, (ids, basura)
    assert basura == []


def test_no_repite_y_conserva_el_ORDEN():
    a, b = "16847caa-9e5b-5b01-8531-2e576240e820", "bfe635f8-ed7d-538d-9c16-76621576eda6"
    ids, _ = rescore.parse_uuids(f"{b} {a} {b}")
    assert ids == [b, a]


def test_lo_que_NO_es_uuid_se_devuelve_aparte_y_no_se_traga():
    """Un id mal pegado tiene que VERSE. Tragarlo en silencio es encolar de menos y creer
    que se encolo todo."""
    ids, basura = rescore.parse_uuids("16847caa-9e5b-5b01-8531-2e576240e820, hola, 123")
    assert len(ids) == 1
    assert basura == ["hola", "123"]


def test_normaliza_a_minusculas():
    ids, _ = rescore.parse_uuids("16847CAA-9E5B-5B01-8531-2E576240E820")
    assert ids == ["16847caa-9e5b-5b01-8531-2e576240e820"]


# --- clasificar: ¿esto es una interaccion, una sesion o un ticket? ----------

def test_clasifica_cada_id_por_la_COLUMNA_en_la_que_existe():
    cur = _FakeCursor({"interaccion_id": {"aaaaaaaa-0000-4000-8000-000000000001"},
                       "conversation_id": {"bbbbbbbb-0000-4000-8000-000000000002"},
                       "ticket_id": {"cccccccc-0000-4000-8000-000000000003"}})
    r = rescore.clasificar(cur, ["aaaaaaaa-0000-4000-8000-000000000001",
                                 "bbbbbbbb-0000-4000-8000-000000000002",
                                 "cccccccc-0000-4000-8000-000000000003"])
    assert r["interaccion"] == ["aaaaaaaa-0000-4000-8000-000000000001"]
    assert r["sesion"] == ["bbbbbbbb-0000-4000-8000-000000000002"]
    assert r["ticket"] == ["cccccccc-0000-4000-8000-000000000003"]
    assert r["sin_match"] == []


def test_un_id_que_no_existe_en_NINGUNA_columna_se_reporta():
    """Es lo mas probable que pase: un uuid de otra base, o de una fila que todavia no se
    scoreo. Si no se avisa, el operador cree que encolo algo que no encolo."""
    cur = _FakeCursor({"interaccion_id": set()})
    r = rescore.clasificar(cur, ["dddddddd-0000-4000-8000-000000000004"])
    assert r["sin_match"] == ["dddddddd-0000-4000-8000-000000000004"]


def test_un_id_se_cuenta_UNA_sola_vez_aunque_matchee_dos_columnas():
    """No deberia pasar --son espacios de nombres distintos-- pero si pasa, contarlo dos
    veces infla el numero que la persona usa para decidir."""
    igual = "aaaaaaaa-0000-4000-8000-000000000001"
    cur = _FakeCursor({"interaccion_id": {igual}, "conversation_id": {igual}})
    r = rescore.clasificar(cur, [igual])
    assert r["interaccion"] == [igual] and r["sesion"] == []


# --- la condicion que se le pasa al UPDATE ----------------------------------

def test_la_condicion_cubre_las_TRES_columnas():
    cond, params = rescore.condicion_por_ids(["aaaaaaaa-0000-4000-8000-000000000001"])
    for col in ("interaccion_id", "conversation_id", "ticket_id"):
        assert f"{col}::text = ANY" in cond, col
    assert params == [["aaaaaaaa-0000-4000-8000-000000000001"]] * 3


def test_sin_ids_NO_hay_condicion():
    """Una condicion vacia se vuelve `WHERE true` y encola la tabla entera: exactamente el
    rescore de 369 dias que esto viene a evitar."""
    assert rescore.condicion_por_ids([]) == (None, None)


def test_la_condicion_compara_como_TEXTO_y_no_como_uuid():
    """Los ids llegan de una caja de texto. Castear la COLUMNA a text --y no el parametro a
    uuid-- evita que un id con una letra de mas reviente el UPDATE entero en vez de salir
    como `sin_match`."""
    cond, _ = rescore.condicion_por_ids(["aaaaaaaa-0000-4000-8000-000000000001"])
    assert "::text" in cond and "::uuid" not in cond


# --- COMO SABER SI LA COLA ESTA AVANZANDO (2026-09-01) ----------------------
#
# Faltaba, y la ausencia era un footgun: la unica forma de contar lo que quedaba pendiente
# era correr `--deshacer` en seco, y un `--aplicar` de mas ahi BORRA la cola entera. Pedir
# el estado de algo no puede compartir comando con destruirlo.

def test_el_estado_es_de_SOLO_LECTURA():
    """Nada de esto puede escribir. Es la razon de existir del comando."""
    junto = " ".join(rescore._ESTADO_SQL.upper().split())
    for peligro in ("UPDATE", "DELETE", "INSERT", "TRUNCATE", "DROP"):
        assert peligro not in junto, peligro


def test_el_estado_separa_lo_PENDIENTE_de_lo_ya_SERVIDO():
    """`scored_at >= rescore_pedido_at` es la condicion de servida, la misma que lee el
    worker en `_notas_de_la_sesion`. Si `pendientes` no baja, la cola no esta corriendo."""
    class _Cur:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            return (7, 3, 254, 115, "2026-09-01T16:00:00", "2026-09-01T15:00:00")
    r = rescore.estado(_Cur())
    assert r["pendientes"] == 7 and r["pendientes_sesiones"] == 3
    assert r["servidas"] == 254 and r["servidas_sesiones"] == 115
    assert r["ultima_servida"] == "2026-09-01T16:00:00"


def test_pedir_el_estado_y_pedir_el_borrado_JUNTOS_se_rechaza():
    """Y se rechaza ANTES de conectarse: si llegara a la base, ya seria tarde."""
    from scripts import pedir_rescore
    assert pedir_rescore.main(["--estado", "--aplicar"]) == 2
    assert pedir_rescore.main(["--estado", "--deshacer"]) == 2
