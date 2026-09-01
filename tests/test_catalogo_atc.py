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
    """Sirven para escribir el coaching en su jerga: "usa R5PLACER" dice más que "mandá un
    mensaje de seguimiento"."""
    for rr in ("BIENVENIDA", "FIN", "R5PLACER", "VISTO", "VERIFICARCUENTA"):
        assert rr in RESPUESTAS_RAPIDAS, rr
    for nombre, para_que in RESPUESTAS_RAPIDAS.items():
        assert para_que, f"{nombre} sin explicación de para qué sirve"


def test_los_nombres_se_copian_VERBATIM_del_crm_sin_inventar_una_barra():
    """Hasta el 2026-08-28 todas llevaban una barra delante y ninguna coincidía con la
    grafía real: el operador la buscaba en Whaticket y no la encontraba. Y la barra NO es un
    prefijo uniforme — en el catálogo real hay shortcuts con ella (`/000`, `/888ALE`) y sin
    ella (`FIN`, `R3RECARGA`), así que ponerla "para que se lea mejor" es inventar un nombre.
    El chequeo contra el CRM de verdad vive en tests/test_catalogo_atc_contra_el_crm.py."""
    inventadas = [n for n in RESPUESTAS_RAPIDAS if n.startswith("/")]
    assert not inventadas, (
        f"estas llevan una barra que hay que verificar contra `fast_responses` antes de "
        f"mostrarla: {inventadas}"
    )


def test_R3RECARGA_no_se_describe_como_la_carga_en_curso():
    """Su texto real dice "Tu saldo ya está disponible": es la acreditación CONSUMADA.
    Describirla como "en curso" empujaba al operador a mandarla antes de acreditar — le
    miente al cliente Y hace que `operator_acreditacion` marque acredito=True sin plata
    entrada. El momento "en curso" lo cubren R1SOLICITUDDECARGA y R2VERIFICACIONDEBOLETA."""
    assert "en curso" not in RESPUESTAS_RAPIDAS["R3RECARGA"].lower()
    assert "en curso" in RESPUESTAS_RAPIDAS["R1SOLICITUDDECARGA"].lower() or \
           "proceso" in RESPUESTAS_RAPIDAS["R1SOLICITUDDECARGA"].lower()


# --- UNA PRACTICA TIENE DOS CARAS (2026-09-01) ------------------------------
#
# BUG REPORTADO POR EL NEGOCIO: *"sale B10 cumplió con los tiempos cuando sale deficiente
# en ese sentido"*. En el detalle de una nota de 2 estrellas por tardar, debajo del titulo
# **Recomendacion**, aparecia un chip VERDE (`class="ok"`, color `--r-buena`) que decia
# "B10 cumplió los tiempos". La rubrica lo castigaba por lento y la pantalla lo felicitaba
# por rapido, en la misma tarjeta.
#
# LA CAUSA: `PRACTICAS` se escribio como "el espejo positivo, para aciertos[]", asi que sus
# `chip` estan en PASADO -- son logros ("cumplió los tiempos", "leyó todo antes de
# responder"). Pero `catalogo_coaching` usa el MISMO codigo para decir a que practica APUNTA
# un consejo, que es lo contrario: lo que falta trabajar. Un solo texto para los dos usos no
# alcanza, porque el sentido se invierte.
#
# `foco` es la otra cara: la practica en INFINITIVO, que es como se nombra un objetivo.

def test_toda_practica_tiene_las_DOS_caras():
    from src.catalogo_atc import PRACTICAS
    for p in PRACTICAS:
        assert p.chip, p.codigo
        assert p.foco, f"{p.codigo} no tiene la forma de FOCO y se mostraria en pasado"
        assert p.chip != p.foco, f"{p.codigo}: el logro y el foco no pueden ser el mismo texto"


def test_el_foco_NO_esta_en_pasado_y_el_logro_SI():
    """La prueba que ataja el bug: un objetivo no se enuncia como algo ya cumplido.

    En español la tilde en la última sílaba marca el pretérito de tercera persona
    ('cumplió', 'leyó', 'aplicó'). Un `foco` con esa forma es exactamente el defecto.
    """
    import re
    pasado = re.compile(r"\b\w*[áéíóú]\b", re.IGNORECASE)
    for p in PRACTICAS:
        assert not pasado.search(p.foco), \
            f"{p.codigo}: el foco '{p.foco}' está en pasado y se lee como un logro"
    assert any(pasado.search(p.chip) for p in PRACTICAS), \
        "los chips SI son logros en pasado: si esto falla, se cambió la cara equivocada"


def test_B10_es_el_caso_que_lo_destapo():
    porcodigo = {p.codigo: p for p in PRACTICAS}
    assert porcodigo["B10"].chip == "cumplió los tiempos"
    assert porcodigo["B10"].foco == "cumplir los tiempos"


def test_el_catalogo_que_ve_el_tablero_expone_el_foco():
    """Sin esto el front no tiene con que reemplazarlo y sigue pintando el pasado."""
    from src.app import catalogo
    c = catalogo()
    b10 = next(p for p in c["practicas"] if p["codigo"] == "B10")
    assert b10["foco"] == "cumplir los tiempos"
