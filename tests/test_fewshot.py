"""Tests de los ejemplos few-shot del motivo (src/fewshot.py).

POR QUE EXISTE ESTE ARCHIVO. El few-shot era un string suelto y quedo con TRES motivos
sin ningun ejemplo (soporte_cuenta, registro, problema). La comparacion de 3 modelos
mostro 38% de acuerdo en el motivo, y los desacuerdos caian EXACTAMENTE en esos tres.
Estos tests hacen de esa cobertura un CONTRATO: si alguien agrega un motivo y se olvida
del ejemplo, el test rompe antes de que el modelo empiece a adivinar.
"""
import json

import pytest

from src.fewshot import EJEMPLOS_MOTIVO, Ejemplo, formatear_fewshot
from src.rubrics import MOTIVOS


# --- cobertura: el contrato que faltaba ------------------------------------------

def test_TODOS_los_motivos_tienen_al_menos_un_ejemplo():
    cubiertos = {e.motivo for e in EJEMPLOS_MOTIVO}
    faltan = set(MOTIVOS) - cubiertos
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
    for m in MOTIVOS:
        assert m in system
    # Un texto que SOLO existe en los ejemplos nuevos: si el prompt sigue usando el
    # string viejo, esto falla. Sin esto el test pasaba por casualidad.
    assert "nathaly365" in system, "el prompt no esta usando src/fewshot.py"
    assert "Agencia Burkina" in system
