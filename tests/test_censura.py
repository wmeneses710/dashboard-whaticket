"""Tests de src/censura.py: enmascarar dato sensible EN LA SALIDA.

POR QUE EXISTE. El tablero se sirve en un dominio publico y tiene 14 endpoints de
lectura ANONIMOS que devuelven `customer_number` y transcripts completos (auditoria del
2026-08-24). MEDIDO sobre los 52.135 mensajes de las sesiones scoreadas:
    celular EC                    3.734 (7,16%)   cliente 533 · operador 3.201
    10 digitos (cedula o cuenta)  2.186 (4,19%)   cliente 1.288 · operador 898
    cuenta bancaria                 685 (1,31%)
    cedula declarada                612 (1,17%)
    CREDENCIALES EN CLARO           399 (0,77%)   operador 384   <- lo mas grave
    correo                          354 (0,68%)

DONDE VA, Y ES LA DECISION QUE IMPORTA: en la SALIDA, no en la base ni antes del
scoring. Las señales deterministas viven de estos mismos digitos --  `es_traspaso`
compara el tail del telefono contra el mapa de lineas, `_es_traspaso_de_datos` busca el
email o la cedula, `operator_sent_credentials` busca el patron de credenciales,
`datos_completos_del_alta` exige correo Y digitos--. Censurar antes de calificar no
protege a nadie y rompe la mitad de las rubricas.

LOS MONTOS NO SE CENSURAN A PROPOSITO: "$5" no identifica a nadie y el supervisor lo
necesita para juzgar un deposito. Un dato que no expone y que hace falta no se tapa.
"""
from src.censura import censurar_texto, enmascarar


# --- la forma que pidio el negocio: 8*******1 --------------------------------

def test_enmascarar_deja_el_primero_y_el_ultimo():
    assert enmascarar("812345671") == "8*******1"
    assert enmascarar("0967159807") == "0********7"


def test_enmascarar_no_revela_los_tokens_muy_cortos():
    # Con 4 caracteres, mostrar los dos extremos revela la mitad. Se tapa entero.
    assert enmascarar("1234") == "****"
    assert enmascarar("12") == "**"


def test_enmascarar_conserva_el_largo():
    """El largo es informacion util y NO identifica: distingue una cedula de 10 de un
    celular de 10 de una cuenta de 8, y deja ver que el dato estaba ahi."""
    for s in ("0967159807", "812345671", "12345678"):
        assert len(enmascarar(s)) == len(s)


# --- que censura el texto ----------------------------------------------------

def test_censura_el_celular_y_la_cedula():
    out = censurar_texto("mi numero es 0967159807 y mi cedula 1712345678")
    assert "0967159807" not in out and "1712345678" not in out
    assert "0********7" in out and "1********8" in out
    assert "mi numero es" in out and "mi cedula" in out   # el texto sigue legible


def test_censura_el_correo_pero_deja_ver_que_es_un_correo():
    out = censurar_texto("mandame a ericklopezjosediaz@gmail.com")
    assert "ericklopezjosediaz" not in out
    assert "@" in out, "sin el arroba el lector no sabe que habia un correo"


def test_censura_las_credenciales_en_claro():
    """384 mensajes de operadores entregan usuario y clave por chat. Es el dato mas
    sensible del corpus: con eso se entra a la cuenta de una persona."""
    out = censurar_texto("su usuario es Paula2026 y la contraseña es Sorti2026.")
    assert "Paula2026" not in out
    assert "Sorti2026" not in out
    assert "usuario" in out and "contraseña" in out


def test_NO_censura_los_montos():
    out = censurar_texto("la recarga minima es de $5 y el bono es de $5,00")
    assert "$5" in out


def test_NO_toca_el_texto_comun():
    limpio = "Hola amiga, con gusto te ayudo con tu registro"
    assert censurar_texto(limpio) == limpio


def test_es_idempotente():
    """La salida ya censurada puede volver a pasar por aca (un endpoint que compone
    otro). Enmascarar dos veces no puede comerse mas caracteres."""
    una = censurar_texto("mi numero es 0967159807")
    assert censurar_texto(una) == una


def test_tolera_none_y_vacio():
    assert censurar_texto(None) == ""
    assert censurar_texto("") == ""


# --- PEDIR NO ES ENTREGAR ----------------------------------------------------
#
# Mi primera version tapaba lo que seguia a "usuario"/"contraseña" SIEMPRE, y sobre los
# mensajes reales de la copia se comia palabras comunes:
#     "me ayudas con tu usuario porfa"       -> tapaba "porfa"
#     "Ayudame con tu usuario bro porfa"     -> tapaba "bro"
#     "Su contraseña es tal cual su usuario" -> tapaba "tal"
# La distincion YA estaba resuelta en el repo: `signals.CREDENTIALS_PATTERN` exige el
# orden `su X es` / `usuario: X`, y su comentario lo dice explicito -- "me ayuda con su
# usuario", "cual es su clave" e "indiqueme su usuario" NO matchean. Se reusa esa
# compuerta en vez de inventar otra.

def test_pedir_el_usuario_no_tapa_la_palabra_que_sigue():
    for pedido in ("Hola que tal me ayudas con tu usuario porfa",
                   "Ayudame con tu usuario bro porfa",
                   "tienes el usuario de tu cuenta?",
                   "me ayudas con tu usuario porfa junto a tu correo"):
        assert censurar_texto(pedido) == pedido, pedido


def test_entregar_el_usuario_SI_lo_tapa():
    out = censurar_texto("Muy bien su usuario es Paula2026 y la contraseña es Sorti2026.")
    assert "Paula2026" not in out and "Sorti2026" not in out


def test_la_forma_con_dos_puntos_tambien_se_tapa():
    out = censurar_texto("Tu usuario es: Paula2026\nContraseña: Sorti2026.")
    assert "Paula2026" not in out and "Sorti2026" not in out


def test_la_frase_sin_valor_no_se_toca():
    """`Su contraseña es tal cual su usuario` no entrega nada: describe una regla. Antes
    tapaba "tal" y dejaba la frase incomprensible."""
    frase = "Su contraseña es tal cual su usuario, para que ingrese"
    assert censurar_texto(frase) == frase


def test_es_idempotente_TAMBIEN_con_el_correo():
    """El agujero que el test de idempotencia original no vio, porque probaba con un
    telefono. MEDIDO sobre los 25.282 mensajes distintos de la copia: **345 no eran
    idempotentes**, y todos por el correo.

    LA CAUSA: `*` no esta en `[\\w.+-]`, asi que en la segunda pasada el patron capturaba
    solo los caracteres de palabra pegados al arroba (la `z` final de `e****...z`) y se
    los comia. El dato ya estaba tapado; lo que se degradaba era la senal de que ahi
    habia un correo.
    """
    una = censurar_texto("mandame a ericklopezjosediaz@gmail.com")
    assert censurar_texto(una) == una
    # Y tres pasadas tampoco lo mueven.
    assert censurar_texto(censurar_texto(una)) == una


def test_es_idempotente_con_credenciales_y_digitos():
    for texto in ("su usuario es Paula2026 y la contraseña es Sorti2026.",
                  "mi cedula 1712345678 y mi cuenta 2101059380",
                  "escribime al +593991701676 o al 0967159807"):
        una = censurar_texto(texto)
        assert censurar_texto(una) == una, texto
