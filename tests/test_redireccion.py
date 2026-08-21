"""Tests del traspaso de conversacion a otra linea (`redireccion`).

QUE PROBLEMA RESUELVE. Medido el 2026-08-07 sobre la copia de prod, 671 sesiones de
`sistemas` contienen un traspaso, en tres formas muy distintas:
  B  566  el traspaso es UN mensaje dentro de una conversacion real -> ruido, el motivo
          real manda. Ya funcionaba: no se toca nada.
  C   99  el cliente pidio algo concreto y el traspaso fue TODA la respuesta del
          operador. Estos eran los 2 estrellas: el LLM ve que "no atendio el motivo".
  A    6  el cliente solo dijo "Hola"/"Ok" -> ya los saltea `client_sin_motivo`, y por
          decision del negocio (2026-08-07) SIGUEN etiquetados `sin_motivo`.

La decision del negocio para C: no lleva nota SI el traspaso apunta a una linea nuestra
que esta CONNECTED (el operador ya no puede atender a ese cliente porque el negocio
migro la linea; ponerle 2 lo castiga por algo que no decidio). Si no hay numero, o la
linea esta DISCONNECTED, o no se puede resolver, SI lleva nota: ahi el cliente queda a
la deriva. El caso real que obligo a la condicion: "En caso de no responder, contactate
con esta linea: 0983744476" -> AGENTES OPERATIVOS PRO, DISCONNECTED.
"""
import pytest

from src.redireccion import (
    build_lineas_map,
    es_redireccion_total,
    es_traspaso,
    tails_del_texto,
    traspaso_a_linea_viva,
)
from src.sessions import evaluate_session
from src.signals import client_sin_motivo

# Lineas reales de la copia (tail de 9 digitos -> status), ver connections.
LINEAS = {
    "991194133": "CONNECTED",     # sistemas/Jugadores PLATAFORMA
    "991194168": "CONNECTED",     # datos/MODO 2
    "983744476": "DISCONNECTED",  # sistemas/AGENTES OPERATIVOS PRO
}

# --- deteccion del traspaso: textos REALES de produccion -------------------------

TRASPASOS = [
    "A partir de ahora tus recargas y retiros serán atendidos directamente por el "
    "servicio al cliente de la plataforma😊",
    "Gracias por comunicarte con atención al cliente. Este número se encuentra fuera "
    "de servicio por el momento, por favor comunicarse con el 0991194133",
    "*Anggie Belén:* Estimado usuario 😊 A partir de ahora te estaremos atendiendo "
    "desde el número +593991194133 📲",
    "WhatsApp ha puesto varios de nuestros números en revisión, es por ello que a "
    "partir de ahora les enviaremos un número de atención",
    "Hola, por favor escribir al número de atención al cliente para más info 0991194133",
    "En caso de no responder, contactate con esta línea: 0983744476",
    "Majo: 😊 Si necesitas ayuda con cargas o retiros, puedes escribirme al 📲 0991194168",
]


@pytest.mark.parametrize("texto", TRASPASOS)
def test_reconoce_los_traspasos_reales(texto):
    assert es_traspaso(texto) is True


# Plantillas que NO son traspaso. El primer detector ingenuo se comio estas: la de
# cortesia sola aparece 2.121 veces en `sistemas`, o sea que habria sido el patron mas
# "frecuente" del dataset sin ser una redireccion. Misma trampa que inflo el gate de
# deposito un 41,4% por leer el script del operador.
NO_TRASPASOS = [
    "✨ _Gracias por comunicarte con nosotros 🙌, recuerda que siempre estaremos "
    "disponibles para brindarte el mejor servicio_🤝",
    "Mucha suerte hoy, esperamos poder atenderte de nuevo, pronto!🍀🎉 Recuerda que "
    "siempre tenemos un numero alterno para que siempre puedas jugar 0991194133",
    "👋 ¡Hola! como estas te escribo de Sorti365 para retomar el contacto por aquí 😊",
    "*Alex:* aprovecho que nos escribiste para contarte algo buenísimo para tu agencia 🍀",
    "Tu recarga fue atendida por el equipo, saldo disponible",
    "ya te acredito la recarga",
    "",
]


@pytest.mark.parametrize("texto", NO_TRASPASOS)
def test_no_confunde_cortesia_ni_venta_con_traspaso(texto):
    assert es_traspaso(texto) is False


# --- extraccion y normalizacion del numero --------------------------------------

def test_normaliza_a_los_ultimos_9_digitos():
    # Los mensajes escriben el numero en local (0991194168) y `connections` lo guarda
    # con pais (593991194168): el tail de 9 es lo que los hace comparables.
    assert "991194168" in tails_del_texto("escribime al 0991194168")
    assert "991194133" in tails_del_texto("desde el número +593991194133")


def test_normaliza_numeros_con_espacios_y_signos():
    assert "994995251" in tails_del_texto("Comunicate al numero +593 99 499 5251")


def test_ignora_cifras_que_no_son_telefono():
    # Un monto o una cifra corta no puede producir un tail de 9.
    assert tails_del_texto("recarga de 13 dolares") == set()
    assert tails_del_texto("son 250") == set()


# --- la linea de destino: viva, muerta o desconocida -----------------------------

def _op(body):
    return {"from_me": True, "is_note": False, "body": body, "sent_from": "OPERATOR"}


def _cli(body, media="chat"):
    return {"from_me": False, "is_note": False, "body": body, "media_type": media}


def test_linea_viva_reconocida():
    msgs = [_cli("cargame 13"), _op("A partir de ahora te estaremos atendiendo desde "
                                   "el número +593991194133")]
    assert traspaso_a_linea_viva(msgs, LINEAS) is True


def test_linea_DESCONECTADA_no_cuenta_como_traspaso_valido():
    # El caso que obligo a la condicion: manda al cliente a una linea muerta.
    msgs = [_cli("cargame 13"),
            _op("En caso de no responder, contactate con esta línea: 0983744476")]
    assert traspaso_a_linea_viva(msgs, LINEAS) is False


def test_numero_que_no_es_de_ninguna_linea_nuestra():
    msgs = [_cli("cargame 13"), _op("escribime al 0999999999")]
    assert traspaso_a_linea_viva(msgs, LINEAS) is False


def test_traspaso_SIN_numero_no_es_valido():
    # "comunicate con atencion al cliente" y nada mas: el cliente no sabe adonde ir.
    msgs = [_cli("cargame 13"),
            _op("por favor escribir al número de atención al cliente")]
    assert traspaso_a_linea_viva(msgs, LINEAS) is False


def test_sin_mapa_de_lineas_no_se_puede_confirmar_nada():
    # Falla del lado seguro: sin el mapa no se regala un skip.
    msgs = [_cli("cargame 13"), _op("te atendemos desde el +593991194133")]
    assert traspaso_a_linea_viva(msgs, None) is False
    assert traspaso_a_linea_viva(msgs, {}) is False


# --- redireccion TOTAL: el traspaso es toda la respuesta (bucket C) --------------

def test_bucket_C_el_traspaso_es_toda_la_respuesta():
    msgs = [_cli("Buenas para recargar 5"),
            _op("A partir de ahora tus recargas y retiros serán atendidos directamente "
                "por el servicio al cliente de la plataforma, escribe al 0991194133")]
    assert es_redireccion_total(msgs, LINEAS) is True


def test_bucket_B_complementario_NO_es_redireccion_total():
    # El operador atendio Y ademas paso el numero: el motivo real manda, no se saltea.
    msgs = [_cli("no me llego la recarga"),
            _op("ya te la acredito, saldo disponible"),
            _op("igual a partir de ahora te atendemos desde el 0991194133")]
    assert es_redireccion_total(msgs, LINEAS) is False


def test_sin_mensajes_del_operador_no_es_redireccion():
    assert es_redireccion_total([_cli("hola?")], LINEAS) is False


def test_redireccion_total_exige_que_la_linea_este_viva():
    msgs = [_cli("cargame 13"),
            _op("contactate con esta línea: 0983744476")]
    assert es_redireccion_total(msgs, LINEAS) is False


# --- integracion con la decision de skip ----------------------------------------

def test_la_redireccion_valida_YA_NO_se_saltea():
    """Cambio del 2026-08-20: `redireccion` es un motivo con nota determinista, no un skip.
    La proteccion contra el 2 estrellas se mudo del gate a la rubrica -- ver
    tests/test_redireccion_motivo.py::test_la_linea_viva_no_es_una_falla."""
    msgs = [_cli("Buenas para recargar 5"),
            _op("A partir de ahora te estaremos atendiendo desde el 0991194133")]
    _, _, eval_status, skip_reason = evaluate_session(msgs, lineas=LINEAS)
    assert (eval_status, skip_reason) == ("evaluated", None)


def test_la_redireccion_a_linea_muerta_SI_se_evalua():
    msgs = [_cli("Buenas para recargar 5"),
            _op("contactate con esta línea: 0983744476")]
    _, _, eval_status, skip_reason = evaluate_session(msgs, lineas=LINEAS)
    assert eval_status == "evaluated"


def test_la_cortesia_le_sigue_ganando_al_traspaso():
    """La PRIORIDAD se conserva, cambio DONDE se expresa. Hasta el 2026-08-21 `sin_motivo`
    era un skip y ganaba en `evaluate_session`; ahora se evalua (estandar de cierre, ver
    src/solo_cortesia.py) y la prioridad vive en el ORDEN del worker, que chequea
    `client_sin_motivo` ANTES de `respuesta_fue_solo_traspaso`. Lo que este test fija es la
    señal que sostiene esa prioridad."""
    # Decision del negocio (2026-08-07): el bucket A se queda en `sin_motivo`.
    msgs = [_cli("Hola"),
            _op("A partir de ahora te estaremos atendiendo desde el 0991194133")]
    _, _, eval_status, skip_reason = evaluate_session(msgs, lineas=LINEAS)
    # La señal es lo que sostiene la prioridad, y el worker la chequea antes que el
    # traspaso. La sesion ya no se saltea: lleva nota por el estandar de cierre.
    assert client_sin_motivo(msgs) is True
    assert (eval_status, skip_reason) == ("evaluated", None)


def test_evaluate_session_sin_lineas_no_cambia_nada():
    # Compatibilidad: quien no pasa el mapa ve exactamente la conducta anterior.
    msgs = [_cli("Buenas para recargar 5"),
            _op("A partir de ahora te estaremos atendiendo desde el 0991194133")]
    _, _, eval_status, _ = evaluate_session(msgs)
    assert eval_status == "evaluated"


# --- el mapa de lineas desde la BD ----------------------------------------------

class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, q, p=None):
        self.executed.append((q, p))

    def fetchall(self):
        return self._rows


def test_build_lineas_map_normaliza_y_conserva_el_status():
    cur = _Cur([("593991194133", "CONNECTED"), ("0983744476", "DISCONNECTED"),
                ("", "CONNECTED"), (None, "CONNECTED"), ("123", "CONNECTED")])
    m = build_lineas_map(cur)
    assert m == {"991194133": "CONNECTED", "983744476": "DISCONNECTED"}


def test_build_lineas_map_prefiere_CONNECTED_ante_el_mismo_numero():
    # El mismo numero puede aparecer en dos filas de `connections` (hay duplicados por
    # nombre). Si alguna esta viva, la linea esta viva.
    cur = _Cur([("593991194133", "DISCONNECTED"), ("593991194133", "CONNECTED")])
    assert build_lineas_map(cur) == {"991194133": "CONNECTED"}


# --- DOS HUECOS DE ACENTO/RAIZ EN EL PATRON ------------------------------------------
# Hallados el 2026-08-12 barriendo las sesiones que pasan una linea NUESTRA viva y que el
# patron NO ve. `re.IGNORECASE` no dobla acentos, y el patron quedo ASIMETRICO: la variante
# `-nos` tiene su forma acentuada y la `-me` no. "Escríbeme al <numero>" es la plantilla de
# migracion Facebook -> WhatsApp, de las mas frecuentes.
# Y "a partir de ahora tu numero principal de ATENCION al Cliente sera" no matcheaba porque
# la alternancia pedia `atend` -- que esta en "atenderemos" y no en "Atención".
# Arreglar los dos lleva los skips de 3 a 9 sobre 3.000 sesiones de la copia.

def test_escribeme_al_matchea_con_acento_y_sin_acento():
    for texto in ("Escríbeme al 0991701676 y conversamos",
                  "Escribeme al 0991701676",
                  "escríbenos al 0991701676",
                  "Escríbanos al 0991701676"):
        assert es_traspaso(texto), texto


def test_la_migracion_institucional_dice_ATENCION_no_atender():
    assert es_traspaso("Te informamos que a partir de ahora tu número principal de "
                       "Atención al Cliente será: 0991701676")
    assert es_traspaso("a partir de ahora te atenderemos por este numero 0991701676")


def test_la_despedida_con_numero_alterno_sigue_NO_siendo_traspaso():
    # El guard que ya existia: es la plantilla de cierre mas comun y no traspasa nada.
    assert not es_traspaso("Mucha suerte hoy, esperamos poder atenderte de nuevo, pronto! "
                          "Recuerda que siempre tenemos un numero alterno 0991701676")
    # Y el operador dando SU PROPIO numero tampoco: no hay traspaso a otro.
    assert not es_traspaso("Estoy a la orden siempre. Escríbeme de una cuando gustes")
