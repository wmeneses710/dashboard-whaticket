"""Identidad del operador: una PERSONA, aunque el CRM le haya dado varios user_id.

POR QUE EXISTE. El CRM BORRA usuarios y `/users` solo lista los vivos, asi que la firma en
el cuerpo del mensaje es la unica fuente de historia (verificado el 2026-08-07: de los 38
huerfanos, la API viva devuelve CERO). Y cuando a una persona le recrean la cuenta, sus
mensajes quedan repartidos entre varios user_id.

Dos consecuencias, las dos serias:
  - las estadisticas por operador quedan PARTIDAS entre filas;
  - `operator_status` (el prender/apagar) matchea por NOMBRE, asi que si dos ids de la
    misma persona resuelven a nombres distintos, apagarla NO la apaga entera.

Dos arreglos, los dos verificados sobre los datos:
  1. HAY DOS FORMATOS DE FIRMA. El conocido `*Nombre:*` y otro sin asteriscos, `Nombre:\\n`.
     El segundo rescata 3 de los 4 operadores que estaban sin nombre.
  2. LA NORMALIZACION TIENE QUE SACAR TILDES. Sin eso "Anahi" y "Anahí" son claves
     distintas: Anahi tiene 3 user_id con 25.290 mensajes y se contaban como 2 con 9.963.
"""
from src.operators import build_operator_map, clave_persona, nombre_de_firma


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, q, p=None):
        self.executed.append((q, p))

    def fetchall(self):
        return self._rows


# --- los dos formatos de firma ---------------------------------------------------

def test_la_firma_con_asteriscos():
    assert nombre_de_firma("*Maria Jose:* buenas, ya te ayudo") == "Maria Jose"


def test_la_firma_SIN_asteriscos():
    # El formato que faltaba. Caso real: rescata a Santiago Angulo (39 msgs).
    assert nombre_de_firma("Santiago Angulo:\nPara una atención más rápida...") == "Santiago Angulo"


def test_la_firma_sin_asteriscos_de_una_sola_palabra():
    assert nombre_de_firma("MODOSORTI:\nEstoy aquí para recordarte...") == "MODOSORTI"


def test_no_confunde_dos_puntos_cualquiera_con_una_firma():
    # Lo que NO puede pasar: que cualquier texto con ':' se lea como nombre.
    for texto in ("Nota: el deposito ya entro",
                  "hola, te comento: ya esta acreditado",
                  "Horario: de 6am a 12pm",
                  "https://sorti.ec/registro",
                  "12:30 te confirmo"):
        assert nombre_de_firma(texto) is None, texto


def test_un_nombre_demasiado_largo_no_es_firma():
    assert nombre_de_firma("Estimado cliente le informamos que su solicitud fue procesada: ok") is None


def test_sin_texto_no_hay_firma():
    assert nombre_de_firma("") is None and nombre_de_firma(None) is None


# --- la clave de persona ---------------------------------------------------------

def test_las_tildes_no_parten_a_la_persona():
    # El bug medido: "Anahi" y "Anahí" contaban como dos personas distintas.
    assert clave_persona("Anahí") == clave_persona("Anahi")


def test_ni_las_mayusculas_ni_los_espacios():
    assert clave_persona("  GENESSIS ") == clave_persona("Genessis")
    assert clave_persona("Annel  Flores") == clave_persona("annel flores")


def test_personas_distintas_NO_se_mezclan():
    # Conservador a proposito: normalizar de mas fusionaria operadores diferentes.
    assert clave_persona("Mario") != clave_persona("Mariana")
    assert clave_persona("Salome Ramirez") != clave_persona("Salome Vera")


# --- el mapa, con los dos formatos -----------------------------------------------

def test_el_mapa_toma_el_nombre_mas_frecuente():
    cur = _Cur([("u1", "Mel", 10), ("u1", "Melany", 3)])
    assert build_operator_map(cur)["u1"] == "Mel"


def test_el_mapa_unifica_las_variantes_con_tilde():
    # Dos user_id de la MISMA persona escritos distinto -> mismo nombre para los dos, asi
    # el prender/apagar y las estadisticas la tratan como una sola.
    cur = _Cur([("u1", "Anahí", 100), ("u2", "Anahi", 20)])
    m = build_operator_map(cur)
    assert m["u1"] == m["u2"], m
