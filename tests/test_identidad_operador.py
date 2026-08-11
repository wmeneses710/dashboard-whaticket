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
from src.operators import (
    build_operator_map,
    clave_persona,
    es_nombre_de_persona,
    nombre_de_firma,
    operator_name,
)


class _Cur:
    """Cursor minimo. `build_operator_map` hace DOS consultas (firmas y catalogo), asi que
    recibe una lista de result-sets y devuelve uno por cada execute()."""

    def __init__(self, *result_sets):
        self._sets = list(result_sets) or [[]]
        self.executed = []

    def execute(self, q, p=None):
        self.executed.append((q, p))
        self._actual = self._sets.pop(0) if self._sets else []

    def fetchall(self):
        return self._actual


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
    cur = _Cur([("u1", "Mel", 10), ("u1", "Melany", 3)], [])
    assert build_operator_map(cur)["u1"] == "Mel"


def test_el_mapa_unifica_las_variantes_con_tilde():
    # Dos user_id de la MISMA persona escritos distinto -> mismo nombre para los dos, asi
    # el prender/apagar y las estadisticas la tratan como una sola.
    cur = _Cur([("u1", "Anahí", 100), ("u2", "Anahi", 20)], [])
    m = build_operator_map(cur)
    assert m["u1"] == m["u2"], m


# --- UNA FRASE NO ES UNA FIRMA (regresion de 084bf60) ----------------------------
# El formato 2 (`Nombre:\n`) se agrego para rescatar 3 operadores sin nombre, pero tambien
# empezo a leer encabezados de PLANTILLA como si fueran firmas: son <=3 palabras y el ':'
# cierra la linea, o sea cumplen los tres guardas que el comentario declaraba "angostos".
# Medido el 2026-08-11 sobre la copia de prod: 4 user_id resolvian a 3 operadores FANTASMA
# y arrastraban 7.425 sesiones mal atribuidas, con el nombre real disponible en `users`.

def test_una_frase_de_plantilla_NO_es_un_nombre():
    # Los tres fantasmas reales, con su plantilla:
    #   "Monto a retirar:\nbanco:\nnumero de cuenta:..."  (formulario de retiro)
    #   "Te llevas:\n• Freebet de $5...\n• 10 giros..."    (lista de promo)
    #   "Te doy:\n$5 de Freebet.\n10 giros gratis."        (idem)
    for frase in ("Monto a retirar", "Te doy", "Te llevas", "Te llevarias",
                  "Nombre de usuario", "Numero de celular", "Formulario de retiro",
                  "Condiciones del Bono", "Con estos requisitos", "Ingrese con esta",
                  "La nueva cuenta", "Le coloque", "envíame por favor",
                  "Claro Te llevas", "Sii Te llevas"):
        assert es_nombre_de_persona(frase) is False, frase
        assert nombre_de_firma(f"{frase}:\nlo que sea") is None, frase


def test_los_nombres_de_persona_reales_siguen_pasando():
    # Verificado contra TODAS las firmas y todo el catalogo `users`: la regla no caza un
    # solo nombre de persona. No hay ningun "Maria de los Angeles" en estos datos.
    for nombre in ("Maria Jose", "Melanie", "Annel Flores", "Santiago Angulo",
                   "MODOSORTI", "Anahí", "Majo", "Mel", "Ana", "Mario", "Genessis",
                   "Andree Rodriguez", "Josue Escudero"):
        assert es_nombre_de_persona(nombre) is True, nombre
        assert nombre_de_firma(f"*{nombre}:* hola") == nombre, nombre


def test_el_mapa_descarta_la_firma_basura_y_usa_el_catalogo():
    # El caso de Gloria Villacis: la firma dominante de ese user_id era "Monto a retirar"
    # (20 msgs del formulario) porque el operador manda desde WEB sin prefijo de firma.
    # `users` tiene el nombre correcto -> se usa ese en vez de dejarlo sin nombre.
    cur = _Cur([("u1", "Monto a retirar", 20)],
               [("u1", "Andree Rodriguez")])
    assert build_operator_map(cur)["u1"] == "Andree Rodriguez"


def test_dos_operadores_distintos_dejan_de_colapsar_en_un_fantasma():
    # "Te doy" ganaba en DOS user_id de personas distintas (Annel Flores y Genessis) y
    # `clave_persona` los unificaba en un solo operador inexistente.
    cur = _Cur([("u1", "Te doy", 36), ("u2", "Te doy", 1)],
               [("u1", "Annel Flores"), ("u2", "Genessis")])
    m = build_operator_map(cur)
    assert m["u1"] == "Annel Flores" and m["u2"] == "Genessis", m


def test_el_huerfano_sin_catalogo_conserva_su_firma():
    # La razon de ser de las firmas: el CRM BORRA usuarios y `/users` no los devuelve.
    # 37 user_id estan solo en las firmas -> si no hay catalogo, la firma manda.
    cur = _Cur([("u1", "Santiago Angulo", 39)], [])
    assert build_operator_map(cur)["u1"] == "Santiago Angulo"


def test_el_catalogo_define_la_GRAFIA_cuando_es_el_mismo_nombre():
    # Caso real: la firma dice 'onlysorti' y `users` dice 'OnlySorti'. `clave_persona` ya
    # los trata como la misma persona, asi que lo unico en juego es como se ESCRIBE — y ahi
    # manda el catalogo, que es el sistema de registro. Solo aplica cuando el nombre es el
    # MISMO salvo mayusculas/tildes: nunca cambia un nombre por otro.
    cur = _Cur([("u1", "onlysorti", 500)], [("u1", "OnlySorti")])
    assert build_operator_map(cur)["u1"] == "OnlySorti"


def test_dos_grafias_en_el_catalogo_se_desempatan_determinista():
    # Caso real: la MISMA marca existe una vez por cuenta, con distinta grafia —
    # 'onlysorti' en `sistemas` y 'OnlySorti' en `datos`. Sin regla explicita ganaba la
    # ultima fila que devolvia la BD, o sea el nombre del dashboard cambiaba de corrida en
    # corrida. Gana la forma con mayusculas, que es la que se muestra.
    assert build_operator_map(
        _Cur([], [("u1", "onlysorti"), ("u2", "OnlySorti")]))["u1"] == "OnlySorti"
    # y al reves, para probar que NO depende del orden de las filas
    assert build_operator_map(
        _Cur([], [("u1", "OnlySorti"), ("u2", "onlysorti")]))["u1"] == "OnlySorti"


def test_una_firma_de_persona_le_GANA_al_catalogo():
    # LIMITE DELIBERADO del arreglo. "Maria Jose" (56.373 msgs firmados) vs `users` que
    # dice "Majo": es la misma persona con dos grafias, no un fantasma. Igual "Melanie"
    # (16.160) contra "Romina", que puede ser un seudonimo comercial: imponer el catalogo
    # ahi le atribuiria el trabajo a otra persona. El catalogo entra solo cuando la firma
    # NO es un nombre de persona; las grafias legitimas se deciden con el negocio.
    cur = _Cur([("u1", "Maria Jose", 56373)], [("u1", "Majo")])
    assert build_operator_map(cur)["u1"] == "Maria Jose"


# --- operator_name: la firma DENTRO de una sesion --------------------------------
# Estos cuatro venian de tests/test_operators.py, que se elimino: sus otros 3 tests
# duplicaban los de arriba y su docstring afirmaba "la tabla users viene vacia", que
# dejo de ser cierto (48 usuarios, y `build_operator_map` ahora usa el catalogo).

def test_operator_name_solo_mira_los_mensajes_de_ESE_operador():
    msgs = [{"from_me": True, "is_note": False, "user_id": "op-B", "body": "*Otro:*\nhi"}]
    assert operator_name(msgs, "op-A") is None


def test_operator_name_sin_firma_es_none():
    msgs = [{"from_me": True, "is_note": False, "user_id": "op-A", "body": "hola sin firma"}]
    assert operator_name(msgs, "op-A") is None


def test_operator_name_sin_operador_es_none():
    assert operator_name([], None) is None


def test_el_mapa_se_scopea_por_cuenta():
    cur = _Cur([], [])
    build_operator_map(cur, account="datos")
    assert "datos" in (cur.executed[-1][1] or ())
