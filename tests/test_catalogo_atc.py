"""El catálogo es de ELLOS: los tests protegen que siga siéndolo.

No se testea "que el código funcione" -- son dos tuplas y tres funciones de una línea. Se
testea lo único que puede romperse en serio: que alguien reescriba con nuestras palabras una
frase que es del manual, que se pierda la numeración que el supervisor conoce, o que el
catálogo y el enum del prompt se desincronicen y el modelo empiece a inventar códigos.
"""
import re

from src.catalogo_atc import (
    CODIGOS_ERROR,
    CODIGOS_PRACTICA,
    ERRORES,
    ERROR_POR_CODIGO,
    PRACTICAS,
    RESPUESTAS_RAPIDAS,
    bloque_para_el_prompt,
    chip_de_error,
    texto_de_error,
)


def test_son_los_doce_y_los_doce():
    """El manual publica DOCE errores críticos y DOCE buenas prácticas. Si alguien agrega
    uno propio, el catálogo deja de ser el de ellos y vuelve a ser el nuestro."""
    assert len(ERRORES) == 12
    assert len(PRACTICAS) == 12


def test_la_numeracion_es_la_del_manual_y_esta_completa():
    """El supervisor los conoce por su número ("el error 9 otra vez"). Reordenarlos o saltear
    uno rompe la conversación con el negocio, no el código."""
    assert CODIGOS_ERROR == tuple(f"E{i:02d}" for i in range(1, 13))
    assert CODIGOS_PRACTICA == tuple(f"B{i:02d}" for i in range(1, 13))


def test_no_hay_codigos_repetidos():
    assert len(ERROR_POR_CODIGO) == len(ERRORES)


def test_las_frases_son_las_del_manual():
    """Muestreo de anclas VERBATIM. Si un refactor las "mejora", este test lo caza. La regla
    del módulo es explícita: `texto` no se edita."""
    assert texto_de_error("E02") == "Solicitar información que el cliente ya proporcionó."
    assert texto_de_error("E06") == "Cerrar chats sin seguimiento adecuado o sin despedida."
    assert texto_de_error("E09") == ("No respetar los tiempos de espera y seguimiento "
                                     "establecidos.")
    assert texto_de_error("E10") == ("Alterar respuestas rápidas, protocolos o información "
                                     "oficial.")


def test_toda_frase_del_manual_termina_en_punto_y_arranca_en_infinitivo():
    """La forma del manual: cada error es una ACCIÓN en infinitivo ("Responder sin leer...",
    "Solicitar información...", y con la negación adelante en "No respetar los tiempos").
    Es lo que los hace legibles como lista de faltas y no como reproches sueltos.

    El `*` en vez de `+` no es un detalle: "Dar" es un infinitivo de tres letras y con `+`
    quedaba afuera. Un test de forma que rechaza la forma real no prueba nada."""
    INFINITIVO = re.compile(r"^(No )?[A-ZÁÉÍÓÚa-záéíóú][a-záéíóúñ]*(ar|er|ir)$")
    for f in ERRORES:
        assert f.texto.endswith("."), f.codigo
        palabras = f.texto.split()
        arranque = " ".join(palabras[:2]) if palabras[0] == "No" else palabras[0]
        assert INFINITIVO.match(arranque), \
            f"{f.codigo} no arranca en infinitivo: {arranque!r}"


def test_el_chip_es_corto_porque_va_en_pantalla():
    """La frase del manual es para el detalle; el chip es lo que entra en la fila del
    tablero sin romper el layout."""
    for f in ERRORES:
        assert f.chip and len(f.chip) <= 30, f"{f.codigo}: {f.chip!r}"
        assert f.chip == f.chip.lower() or f.chip[0].islower(), \
            f"{f.codigo}: el chip va en minúscula, es un fragmento y no un título"


def test_cada_error_trae_el_por_que_del_manual():
    """El manual explica cada error debajo del título, y ese "por qué" es lo que convierte
    un reproche en algo accionable. Se muestra en el detalle."""
    for f in ERRORES:
        assert f.detalle and len(f.detalle) > 20, f.codigo


def test_un_codigo_desconocido_no_rompe_el_tablero():
    """Una corrida vieja puede traer un código que ya no existe. Se muestra tal cual: es
    preferible una fila rara a una pantalla en blanco."""
    assert texto_de_error("E99") == "E99"
    assert chip_de_error("cualquier cosa") == "cualquier cosa"


def test_el_bloque_del_prompt_lleva_los_doce_con_su_frase():
    """El modelo necesita el número Y la frase: un código suelto es una etiqueta que cada
    corrida interpreta distinto."""
    bloque = bloque_para_el_prompt()
    for f in ERRORES:
        assert f.codigo in bloque, f.codigo
        assert f.texto in bloque, f.codigo
    assert len(bloque.strip().splitlines()) == 12


def test_las_respuestas_rapidas_son_las_que_el_manual_nombra():
    """Sirven para escribir el coaching en su jerga: "usa /R5PLACER" dice más que "mandá un
    mensaje de seguimiento"."""
    for rr in ("/Bienvenida", "/FIN", "/R5Placer", "/Visto", "/VerificarCuenta"):
        assert rr in RESPUESTAS_RAPIDAS, rr
    for nombre, para_que in RESPUESTAS_RAPIDAS.items():
        assert nombre.startswith("/"), nombre
        assert para_que, f"{nombre} sin explicación de para qué sirve"
