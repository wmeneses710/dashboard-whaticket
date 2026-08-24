"""Tests de los ejemplos few-shot del motivo (src/fewshot.py).

POR QUE EXISTE ESTE ARCHIVO. El few-shot era un string suelto y quedo con TRES motivos
sin ningun ejemplo (soporte_cuenta, registro, problema). La comparacion de 3 modelos
mostro 38% de acuerdo en el motivo, y los desacuerdos caian EXACTAMENTE en esos tres.
Estos tests hacen de esa cobertura un CONTRATO: si alguien agrega un motivo y se olvida
del ejemplo, el test rompe antes de que el modelo empiece a adivinar.
"""
import json
import re

import pytest

from src.fewshot import EJEMPLOS_MOTIVO, Ejemplo, formatear_fewshot
from src.rubrics import MOTIVOS, MOTIVOS_DEL_LLM


# --- cobertura: el contrato que faltaba ------------------------------------------

def test_TODOS_los_motivos_tienen_al_menos_un_ejemplo():
    """Solo los que el modelo puede ELEGIR. `redireccion` la decidimos nosotros con
    `connections`, no se le pregunta, y por eso no necesita ejemplo few-shot."""
    from src.rubrics import MOTIVOS, MOTIVOS_DEL_LLM

    cubiertos = {e.motivo for e in EJEMPLOS_MOTIVO}
    faltan = set(MOTIVOS_DEL_LLM) - cubiertos
    assert not faltan, f"motivos sin ejemplo few-shot: {sorted(faltan)}"


def test_ningun_motivo_acapara_los_ejemplos():
    # El few-shot viejo tenia 3 de 8 en `deposito`, que YA es la clase mayoritaria:
    # reforzaba el sesgo en vez de corregirlo.
    from collections import Counter
    c = Counter(e.motivo for e in EJEMPLOS_MOTIVO)
    tope = max(c.values())
    assert tope <= len(EJEMPLOS_MOTIVO) / 3, \
        f"un motivo concentra {tope} de {len(EJEMPLOS_MOTIVO)} ejemplos: {c.most_common()}"


def test_los_motivos_de_los_ejemplos_son_validos():
    for e in EJEMPLOS_MOTIVO:
        assert e.motivo in MOTIVOS, f"motivo invalido en un ejemplo: {e.motivo!r}"


# --- forma de cada ejemplo ------------------------------------------------------

def test_cada_ejemplo_tiene_transcript_y_porque():
    for e in EJEMPLOS_MOTIVO:
        assert e.transcript.strip(), f"ejemplo sin transcript: {e.motivo}"
        assert e.porque.strip(), f"ejemplo sin explicacion: {e.motivo}"


def test_los_hechos_de_cada_ejemplo_son_serializables_y_completos():
    # El modelo imita la FORMA de la salida, asi que los hechos del ejemplo tienen que
    # tener las mismas claves que el schema pide como requeridas.
    requeridos = {"atendio_el_motivo", "hizo_accion_extra",
                  "cortesia_destacada", "hubo_maltrato_grave"}
    for e in EJEMPLOS_MOTIVO:
        assert requeridos <= set(e.hechos), \
            f"faltan hechos en el ejemplo de {e.motivo}: {requeridos - set(e.hechos)}"
        json.dumps(e.hechos)   # revienta si no es serializable


def test_los_booleanos_son_booleanos_de_verdad():
    # Un "true" string en el ejemplo le enseña al modelo a devolver strings.
    for e in EJEMPLOS_MOTIVO:
        for k in ("atendio_el_motivo", "hizo_accion_extra",
                  "cortesia_destacada", "hubo_maltrato_grave"):
            assert isinstance(e.hechos[k], bool), \
                f"{e.motivo}.{k} no es bool: {e.hechos[k]!r}"


def test_claridad_si_esta_es_del_enum():
    for e in EJEMPLOS_MOTIVO:
        if "claridad" in e.hechos:
            assert e.hechos["claridad"] in ("claro", "confuso", "dudoso")


# --- las trampas que los ejemplos DEBEN enseñar ---------------------------------

def test_hay_un_ejemplo_donde_el_cliente_manda_media_y_NO_es_deposito():
    # La trampa que hizo fallar a lfm2.5:8b: leyo `deposito` en un soporte de clave
    # porque el cliente adjunto una captura. Media del cliente != comprobante.
    ejemplos = [e for e in EJEMPLOS_MOTIVO
                if e.motivo != "deposito" and "[media" in e.transcript.lower()]
    assert ejemplos, "falta el contraste 'cliente manda media pero NO es deposito'"


def test_hay_un_ejemplo_de_disputa_de_apuesta_como_problema():
    # Medido: 3 de 4 disputas de apuesta se clasifican `info` porque el cliente
    # PREGUNTA. El ejemplo tiene que marcar el limite.
    ejemplos = [e for e in EJEMPLOS_MOTIVO if e.motivo == "problema"]
    assert ejemplos, "falta un ejemplo de problema"
    assert any("apuesta" in e.transcript.lower() for e in ejemplos), \
        "el ejemplo de problema deberia ser una disputa de apuesta (el caso real)"


def test_hay_un_ejemplo_de_retiro_donde_el_comprobante_lo_manda_el_operador():
    ejemplos = [e for e in EJEMPLOS_MOTIVO if e.motivo == "retiro"]
    assert ejemplos
    assert any("OPERADOR" in e.transcript and "[media" in e.transcript.lower()
               for e in ejemplos)


# --- formateo -------------------------------------------------------------------

def test_el_bloque_formateado_incluye_todos_los_ejemplos():
    txt = formatear_fewshot()
    for i, e in enumerate(EJEMPLOS_MOTIVO, start=1):
        assert f"[{i}]" in txt
        assert e.motivo in txt


def test_el_bloque_formateado_es_json_valido_por_ejemplo():
    # Cada linea "-> {...}" tiene que ser JSON parseable: si no, le enseñamos al modelo
    # a emitir JSON roto.
    txt = formatear_fewshot()
    bloques = [l for l in txt.splitlines() if l.strip().startswith("-> {")]
    assert len(bloques) == len(EJEMPLOS_MOTIVO)
    for b in bloques:
        json.loads(b.strip()[3:])


def test_el_prompt_real_sigue_usando_el_fewshot():
    # Contrato con src/prompts.py: si alguien desconecta el bloque, el modelo pierde
    # los ejemplos sin que nada falle a la vista.
    from src.prompts import build_motivo_prompt
    system, _ = build_motivo_prompt([{"from_me": False, "body": "hola", "is_note": False}], "")
    assert "EJEMPLOS" in system
    for m in MOTIVOS_DEL_LLM:
        assert m in system
    # Un texto que SOLO existe en los ejemplos nuevos: si el prompt sigue usando el
    # string viejo, esto falla. Sin esto el test pasaba por casualidad.
    assert "nathaly365" in system, "el prompt no esta usando src/fewshot.py"
    assert "Agencia Burkina" in system


# --- LOS CODIGOS DE ERROR DEL MANUAL TIENEN QUE ESTAR EN LOS EJEMPLOS ------------------
# MEDIDO el 2026-08-24 contra el host real (192.168.100.183) con `gemma4:12b`, prompt de
# produccion, 4 sesiones reales de 2 estrellas: `errores` volvio **[] en 4 de 4**, y volvio
# vacio TAMBIEN con `errores` metido en el `required` del schema -- un array vacio cumple.
# En la copia son 0 codigos E en 435 filas, cuando historicamente el 31% producia errores.
#
# LA CAUSA NO ERA EL SCHEMA. `OllamaClient.chat_json` nivel 1 usa `response_format="json"`
# GENERICO (el enum de CODIGOS_ERROR solo ata en el fallback, y el bench dio fallback=0), asi
# que lo unico que pedia los codigos era el prompt. Y el bloque de ejemplos -- 5.263
# caracteres, la parte mas concreta de todo el prompt -- **no mencionaba `errores` ni una
# vez**. Se le estaba enseñando a no ponerlo.
#
# SE MUESTRAN EN TODOS LOS EJEMPLOS, con `[]` en los limpios. Es la distribucion honesta (la
# mayoria de las atenciones no tiene falla) y protege de lo contrario: este repo ya pago caro
# por acusaciones desmedidas, y un few-shot donde el campo SIEMPRE trae codigo enseñaria a
# inventarlos.

def test_cada_ejemplo_declara_sus_errores_aunque_sea_vacio():
    for e in EJEMPLOS_MOTIVO:
        assert isinstance(e.errores, tuple), f"{e.motivo}: errores tiene que ser tupla"


def test_los_codigos_de_los_ejemplos_son_del_catalogo_de_ATC():
    from src.catalogo_atc import CODIGOS_ERROR

    for e in EJEMPLOS_MOTIVO:
        for codigo in e.errores:
            assert codigo in CODIGOS_ERROR, (
                f"{codigo!r} no esta en el catalogo del manual: el modelo copiaria un "
                f"codigo que el front no sabe traducir")


def test_hay_al_menos_dos_ejemplos_CON_codigo_y_varios_sin():
    """Con uno solo el campo se ve como una rareza; con todos, se ensena a inventar."""
    con = [e for e in EJEMPLOS_MOTIVO if e.errores]
    sin = [e for e in EJEMPLOS_MOTIVO if not e.errores]
    assert len(con) >= 2, "el modelo necesita ver el campo USADO mas de una vez"
    assert len(sin) >= 2, "y tambien vacio, o aprende a poner codigo siempre"


def test_el_ejemplo_que_NO_atendio_lleva_codigo():
    """Si el ejemplo mas claro de falla sale con `errores: []`, el bloque sigue ensenando
    que el campo no se usa ni cuando hay algo que reprochar."""
    no_atendieron = [e for e in EJEMPLOS_MOTIVO
                     if e.hechos.get("atendio_el_motivo") is False]
    assert no_atendieron, "hace falta al menos un ejemplo de no-atencion"
    for e in no_atendieron:
        assert e.errores, f"el ejemplo {e.transcript[:40]!r} no atendio y no declara error"


def test_los_errores_van_ANIDADOS_en_dimensions_no_en_la_raiz():
    """La trampa de este cambio: los ejemplos se renderizan PLANOS (`motivo` + los 4
    hechos), asi que meter `errores` ahi le ensenaria al modelo una forma que el schema
    rechaza -- y el codigo lo lee de `raw["dimensions"]["errores"]` (src/scorer.py)."""
    bloque = formatear_fewshot()
    for linea in [x for x in bloque.splitlines() if x.startswith("-> ")]:
        salida = json.loads(linea[3:])
        assert "errores" not in salida, (
            f"`errores` quedo en la raiz del ejemplo: {linea[:90]}")
        assert "dimensions" in salida, "el ejemplo tiene que mostrar el objeto dimensions"
        assert "errores" in salida["dimensions"]


def test_el_bloque_formateado_muestra_codigos_E_de_verdad():
    bloque = formatear_fewshot()
    assert re.search(r"E\d\d", bloque), (
        "el bloque de ejemplos no tiene un solo codigo E: es exactamente el estado que "
        "dejaba `errores` vacio en 435 de 435 filas")


def test_la_forma_del_json_no_ofrece_el_vacio_como_salida_facil():
    """La linea del shape decia `["<codigos E01-E12, o vacio>"]`. Lo ultimo que el modelo
    leia era la invitacion al vacio. El vacio sigue siendo VALIDO -- pero como excepcion."""
    from src.prompts import _MOTIVO_JSON_SHAPE

    assert "E01-E12" in _MOTIVO_JSON_SHAPE
    assert "o vacio" not in _MOTIVO_JSON_SHAPE
