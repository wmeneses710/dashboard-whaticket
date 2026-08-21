"""El vocabulario de cortesía no cubría los typos, y eso costaba 1 estrella.

`_CORTESIA_VOCAB` es un conjunto CERRADO a proposito: alcanza una palabra de verdad para
que el bloque vuelva a exigir respuesta. El diseño esta bien; lo que faltaba es que
"gracias" mal escrito siga siendo "gracias".

MEDIDO el 2026-08-17 sobre la corrida v16, clasificando los 442 pedidos abandonados de las
439 filas de agilidad en 1 estrella: **111 filas (25,3%) tienen como UNICO pedido abandonado
una cortesía o un sticker**. Once formas distintas de "gracias" mal escrito en 185 bloques:

    Graciad · Gracais · Graxias · Grqcias · Graciaas · Graciass · Graciasb · Gracis
    Gra6 Gracias · Mucha agracias · Michas gracias

Y acuses que el vocabulario no nombraba: `Okis`, `Gracias men`, `Ya le escribí`,
`Ok estoy pendiente`, `Me desconecté sin querer`, `Muchas gracias estimado`.

DOS MECANISMOS, y la diferencia importa:

  1. TYPOS -> distancia de edicion 1 contra un nucleo de palabras LARGAS. Un typo es una
     tecla, y generalizar asi mantiene el vocabulario cerrado: "comision" no se acerca a
     ninguna palabra de cortesia. El piso de 5 letras existe para que no colisione con
     palabras cortas de verdad -- sin el, "sale" quedaria a una tecla de "vale" y
     "no me sale" pasaria por cortesía.
  2. PALABRAS QUE FALTABAN COMO CONCEPTO -> se agregan a la lista, y solo las que
     aparecen en los bloques reales: tratamiento (`estimado`, `amigo`, `men`),
     despedida (`hasta`, `manana`, `noche`) y el acuse del agente que avisa que ya hizo
     su parte (`ya le escribí`, `ya le paso`).

EL LIMITE NO SE MUEVE: un pedido sigue siendo un pedido aunque venga con gracias adentro.
Los tests de abajo lo fijan en las dos direcciones.
"""
import random

from src.signals import _a_una_edicion, es_cortesia


# --- 0. el helper de distancia, que es aritmetica de indices ----------------------

def test_a_una_edicion_cubre_las_cuatro_operaciones():
    assert _a_una_edicion("gracias", "gracias") is True   # igual
    assert _a_una_edicion("gracias", "graciad") is True    # sustitucion
    assert _a_una_edicion("gracias", "graciass") is True   # insercion
    assert _a_una_edicion("gracias", "gracis") is True     # borrado
    assert _a_una_edicion("gracias", "gracais") is True    # transposicion
    assert _a_una_edicion("gracias", "grracais") is False  # dos ediciones
    assert _a_una_edicion("", "") is True
    assert _a_una_edicion("", "ab") is False


def test_a_una_edicion_es_simetrica():
    """Es una distancia: si no lo fuera, el resultado dependeria del orden del `any`."""
    random.seed(7)
    for _ in range(2000):
        a = "".join(random.choice("abcdegiors") for _ in range(random.randint(0, 9)))
        b = "".join(random.choice("abcdegiors") for _ in range(random.randint(0, 9)))
        assert _a_una_edicion(a, b) == _a_una_edicion(b, a), (a, b)


# --- 1. los typos de gracias ------------------------------------------------------

def test_los_once_typos_de_gracias_medidos_en_la_copia():
    for typo in ("Graciad", "Gracais", "Graxias", "Grqcias", "Graciaas", "Graciass",
                 "Graciasb", "Gracis", "Graciaas", "Gracias"):
        assert es_cortesia(typo) is True, typo


def test_typos_combinados_con_palabras_del_vocabulario():
    assert es_cortesia("Mucha agracias") is True
    assert es_cortesia("Michas gracias") is True
    assert es_cortesia("Uchas gracias") is True


def test_las_abreviaturas_no_son_typos_y_van_en_la_lista():
    """`grx` y `grcs` estan a 3 y 4 teclas: no las alcanza la distancia 1."""
    assert es_cortesia("Grx") is True
    assert es_cortesia("Grcs") is True


def test_el_piso_de_cinco_letras_protege_las_palabras_cortas():
    """Sin el piso, "sale" queda a una tecla de "vale" y esto seria cortesía."""
    assert es_cortesia("no me sale") is False
    assert es_cortesia("no me sale el saldo") is False


def test_la_distancia_no_alcanza_a_una_palabra_de_verdad():
    assert es_cortesia("comision") is False
    assert es_cortesia("gracias por la comision") is False
    assert es_cortesia("recarga") is False
    assert es_cortesia("gracias recargame") is False


# --- 2. las palabras que faltaban ------------------------------------------------

def test_el_tratamiento_no_convierte_un_agradecimiento_en_pedido():
    assert es_cortesia("Gracias men") is True
    assert es_cortesia("Muchas gracias estimado") is True
    assert es_cortesia("Listo amigo") is True
    assert es_cortesia("Gracias estimados") is True
    assert es_cortesia("Muchas gracias sres") is True


def test_la_despedida_completa():
    assert es_cortesia("Gracias hasta mañana") is True
    assert es_cortesia("Gracias buena noche") is True
    assert es_cortesia("Muchas gracias, buenas noches") is True


def test_que_y_paso_quedaron_afuera_a_proposito():
    """"que paso" es una pregunta y no tiene negacion que la delate, asi que ninguna de las
    dos palabras entra. El precio: "que tengas lindo dia" y "ok ya le paso" no se cubren."""
    assert es_cortesia("que paso") is False
    assert es_cortesia("Muchas gracias, que tengas lindo dia") is False
    assert es_cortesia("Ok ya le paso") is False


def test_la_negacion_da_vuelta_el_acuse():
    """EL CASO QUE CASI ENTRA. Con estas palabras en la lista comun, "no me llegó" -- el
    reclamo mas importante del negocio -- pasaba por cortesía. Se sondeo antes de subirlo."""
    for reclamo in ("no me llego", "no llego nada", "no puedo ingresar", "no funciono",
                    "no salio", "aun no me llega", "todavia no ingresa", "nunca llego"):
        assert es_cortesia(reclamo) is False, reclamo
    # y sin negacion las mismas palabras siguen siendo acuse
    for acuse in ("ya me llego", "ya pude ingresar", "ya funciono", "ya salio"):
        assert es_cortesia(acuse) is True, acuse


def test_la_distancia_no_toca_palabras_del_negocio():
    """Aplicar la distancia a TODO el vocabulario se probo y se descarto: "llego" queda a una
    tecla de "luego" y "saldos" de "saludos". El nucleo son tres palabras por eso."""
    assert es_cortesia("saldo") is False
    assert es_cortesia("saldos") is False
    assert es_cortesia("no me llega el saldo") is False


def test_el_acuse_del_que_avisa_que_ya_hizo_su_parte():
    """El agente no pide nada: informa que la pelota esta de su lado."""
    assert es_cortesia("Ya le escribí") is True
    assert es_cortesia("Ya le escribo") is True
    assert es_cortesia("Ok estoy pendiente") is True
    assert es_cortesia("Okis") is True
    assert es_cortesia("Igualmente muchas gracias") is True
    assert es_cortesia("De nada") is True


def test_el_cliente_diciendo_que_ya_funciono_es_cortesia():
    assert es_cortesia("Gracias ya pude ingresar") is True
    assert es_cortesia("Muchas gracias ya me llego") is True


# --- 3. el limite, que NO se mueve ------------------------------------------------

def test_un_pedido_con_gracias_adentro_sigue_siendo_pedido():
    assert es_cortesia("Gracias, me ayuda con el retiro") is False
    assert es_cortesia("Muchas gracias, una pregunta hoy se hace efectiva mi comision?") is False
    assert es_cortesia("gracias pero no llega el saldo") is False
    assert es_cortesia("Ok gracias esperare mis credenciales") is False


def test_los_pedidos_reales_de_la_copia_siguen_exigiendo_respuesta():
    """Textos textuales de los 78 pedidos abandonados que SI son pedidos."""
    for pedido in ("Me retira 252", "Retírame 1100", "Me ayuda con 34",
                   "Ningún usuario puede ingresar", "No me salen los partidos",
                   "Me puede enviar el link del canal por favor",
                   "Abono 10 a deuda", "Una consulta",
                   "Me podrian volver a pasar la cuenta de gye por favor"):
        assert es_cortesia(pedido) is False, pedido


def test_lo_que_ya_funcionaba_no_se_toca():
    assert es_cortesia("Gracias") is True
    assert es_cortesia("ok") is True
    assert es_cortesia("Muy amable") is True
    assert es_cortesia("buen dia") is True
    assert es_cortesia("") is False
    assert es_cortesia("👍") is True
