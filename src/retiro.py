"""Rubrica DETERMINISTA del motivo `retiro`. Sin LLM y sin BD.

Espeja a src/deposito.py, con la asimetria que define el motivo:

    deposito   el comprobante lo manda el CLIENTE; el operador debe CONFIRMAR
    retiro     el comprobante lo manda el OPERADOR y ES la entrega

Igual que en deposito, el motivo lo clasifica el modelo — eso exige leer intencion —
y la NOTA sale de hechos verificables: cuando pidio la plata, cuando le contestaron,
cuando llego el comprobante, y si chequearon que no faltara nada.

EL CORTE TRANSACCION / CONSULTA. Medido sobre 250 sesiones de retiro (1 por persona,
jul-ago 2026): solo el 43,2% pide plata; el 56,8% pregunta POR el retiro sin pedir
ninguno ("¿como hago para retirar?", "¿cuando me pagan la comision?"). Mezclados, el
"ni acuse ni comprobante" daba 31,6%; separados es 10,2% en la transaccion y 47,9% en
la consulta — que es lo esperable, porque ahi no hay nada que entregar. El separador
es el MONTO: que el cliente diga cuanta plata quiere.

LA ESCALA (definida por el negocio el 2026-08-06):
    5  respuesta <=1 min + comprobante <=15 min + se aseguro de que no faltara nada
    4  respuesta <=1 min + comprobante <=15 min
    3  respuesta 1-5 min, o comprobante 15-30 min
    2  respondio pero nunca mando el comprobante, o tardo de mas
    1  ni respondio ni mando comprobante

UMBRALES, calibrados sobre 108 transacciones: el 74,1% responde en <=2 min y el 86,1%
manda el comprobante dentro de los 15 min del pedido.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from src.interacciones import interaccion_de
from src.operators import inicio_del_reloj
from src.rubrics import formato_espera
from src.catalogo_coaching import consejo_de
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    cliente_tuvo_la_ultima_palabra,
    operator_asked_and_waited,
    operador_derivo_al_agente,
    tiene_reloj,
)
# La espera se mide en HORARIO DE ATENCION (ver src/horario.py): 26 por ciento de los
# deficientes eran clientes que escribieron de madrugada y operadores que contestaron
# ni bien abrio el turno. La noche no es una demora del operador.
from src.horario import espera_efectiva

MODELO_DETERMINISTA = "determinista/retiro-v1"

AGIL = timedelta(minutes=1)             # respuesta inmediata al pedido
RESPUESTA_TOPE = timedelta(minutes=5)   # mas que esto ya no es "rapido"
ENTREGA_AGIL = timedelta(minutes=15)    # comprobante dentro del objetivo
ENTREGA_TOPE = timedelta(minutes=30)    # mas que esto es una demora, no una espera

# El formulario que manda el agente/jugador para cobrar.
_FORMULARIO_RE = re.compile(r"monto a retirar", re.IGNORECASE)
# Plata cerca de una palabra de retiro. El (?<!\d)...(?!\d) es clave: la cedula y el
# telefono viajan en el MISMO formulario y son corridas de 10 digitos, no montos.
_MONTO_RE = re.compile(
    r"(?:monto|retirar|retiro|sacar|cobrar)\D{0,25}(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)(?!\d)",
    re.IGNORECASE)


@dataclass(frozen=True)
class Retiro:
    """Nota determinista de una sesion de retiro."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None       # del pedido a la primera respuesta del operador
    entrega: timedelta | None      # del pedido al comprobante del operador
    pregunto_algo_mas: bool
    derivo_al_agente: bool = False  # el retiro no le correspondia a ATC (ver la rama)


# EL CONSEJO APUNTA A LA RAMA, NO A LA ESTRELLA (misma razon que en src/deposito.py). Al 2
# se llega porque no se envio el comprobante, o porque se envio tarde. Medido el 2026-08-11:
# 112 de las 221 sesiones en 2 estrellas (50,7%) SI habian entregado el comprobante y
# recibian igual el consejo de que "el retiro quedo sin comprobante".
# LOS TEXTOS VIVEN EN src/catalogo_coaching.py (una sola fuente de verdad).


def _pedidos_del_cliente(messages: list[dict]) -> list[dict]:
    """Mensajes del CLIENTE que piden plata (formulario o monto), en orden cronologico."""
    out = []
    for m in sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"]):
        if m.get("from_me"):
            continue
        body = m.get("body") or ""
        if _FORMULARIO_RE.search(body) or _MONTO_RE.search(body):
            out.append(m)
    return out


def _pedido_del_cliente(messages: list[dict]):
    """El ULTIMO pedido del cliente: el que elige la interaccion a juzgar. None si no hay.

    El ANCLA elige la ULTIMA visita: antes tomaba la primera y una sesion mergea todos los
    episodios del ticket (mediana 8,6 h de separacion entre la primera y la ultima, p90 12
    dias, max 266). El 82% de esas sesiones tienen mas de un operador, asi que juzgar la
    primera le cargaba la nota a quien atendio la visita vieja. Ver src/deposito.py para los
    numeros completos. DENTRO de la ventana el reloj arranca en el PRIMER de la visita.
    """
    pedidos = _pedidos_del_cliente(messages)
    return pedidos[-1] if pedidos else None


def es_transaccion(messages: list[dict]) -> bool:
    """El cliente PIDIO plata, no pregunto por el retiro."""
    if not tiene_reloj(messages):
        return False
    return _pedido_del_cliente(messages) is not None


def interaccion_juzgada(messages: list[dict]) -> list[dict] | None:
    """La ventana que `calificar_retiro` va a juzgar. None si no es una transaccion.

    La rubrica ya acotaba su evidencia a la interaccion del pedido; esto lo DICE hacia
    afuera para que los tiempos y el operador que se persisten describan ESA ventana.
    MEDIDO el 2026-08-12: en 152 de 585 sesiones multi-interaccion de deposito/retiro
    (26,0%) la nota se le cargaba a un operador que ni aparece en la interaccion juzgada,
    y 25 de esas son notas de 1 o 2 estrellas -- una mala nota en el legajo de otro.
    """
    pedido = _pedido_del_cliente(messages) if es_transaccion(messages) else None
    return None if pedido is None else interaccion_de(messages, pedido)


def calificar_retiro(messages: list[dict], cierre_at=None, lineas=None) -> Retiro | None:
    """Nota determinista de la sesion. None si no es una transaccion de retiro.

    `lineas`: mapa de nuestras lineas (src/redireccion.build_lineas_map), para reconocer la
    derivacion al agente. Sin el mapa esa rama no se activa: falla del lado seguro.
    """
    if not es_transaccion(messages):
        return None
    pedido = _pedido_del_cliente(messages)
    # LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL PEDIDO (ver src/interacciones.py): la
    # frontera la pone el objeto, la nota de cierre del operador. Sin esto, en las 5.624
    # conversaciones con varios cierres -- el 3,51%, donde vive el 41,7% de los mensajes --
    # el comprobante de entrega de un retiro cerraba el pedido de OTRO. Caso `e5607f47`:
    # mezcla retiros y depositos del 5 al 8-ago y la nota usaba el primer pedido con la
    # evidencia de los siguientes.
    ventana = interaccion_de(messages, pedido)
    # DENTRO de la ventana el reloj arranca en el PRIMERO de ESA visita: el ancla elige la
    # interaccion (la ultima), el reloj mide la espera COMPLETA. Contar desde el ultimo
    # mensaje del tramo esconderia la demora cuando el cliente insiste dos veces seguidas.
    pedido = _pedidos_del_cliente([m for m in ventana if not m.get("is_note")])[0]
    reales = sorted((m for m in ventana if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    posteriores = [m for m in reales if m["created_at"] > pedido["created_at"]]
    respuesta = next((m for m in posteriores if _is_operator(m)), None)
    # La entrega la hace el OPERADOR: una imagen del cliente no acredita nada.
    comprobante = next(
        (m for m in posteriores
         if _is_operator(m) and is_real_media(m.get("media_type"))), None)
    # EL RELOJ ARRANCA CUANDO EL OPERADOR PUEDE RESPONDER (ver src/operators.inicio_del_reloj):
    # la espera EN COLA no es suya. Los dos relojes del motivo arrancan en el mismo punto.
    inicio = inicio_del_reloj(ventana, pedido["created_at"])
    espera = (espera_efectiva(inicio, max(respuesta["created_at"], inicio))
              if respuesta else None)
    entrega = (espera_efectiva(inicio, max(comprobante["created_at"], inicio))
               if comprobante else None)
    algo_mas = operator_asked_and_waited(reales, cierre_at)
    colgado = cliente_tuvo_la_ultima_palabra(reales, cierre_at)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None and comprobante is None:
        return Retiro(1, "mala", "El agente pidió el retiro y nadie le respondió.",
                      None, None, algo_mas)
    if entrega is None and (derivacion := operador_derivo_al_agente(reales, lineas)):
        # LA RAMA DE LA DERIVACION AL AGENTE, espejo de la de src/deposito.py. El manual de
        # ATC (cap. 06) prohibe a ATC procesar recargas Y RETIROS de un jugador que pertenece
        # a un agente, asi que exigirle el comprobante de pago es exigirle el paso prohibido.
        # Techo en 4 por la misma razon que alla: el 5 es el mejor escenario del motivo, y un
        # retiro que ATC no podia pagar no lo es.
        # EL RELOJ DE ESTA RAMA NO ES `AGIL`, y la razon es del manual, no de los datos: antes
        # de derivar, el operador TIENE que pedir el usuario y verificar en el sistema a que
        # agencia pertenece (cap. 05, "Solicitud directa de cuenta bancaria", pasos 1 y 2).
        # Eso es una CONSULTA, no un reflejo, y el minuto de `AGIL` mide la primera respuesta.
        # Cobrarle el minuto seria cobrarle la verificacion que el manual le exige hacer.
        # Se usa el mismo tope que la rama del alta imposible de src/registro.py (5 min para
        # un aviso que requiere mirar el sistema). MEDIDO sobre los 18 casos que la señal
        # encuentra: p50 4,3 min, 56% dentro de 5 y solo 11% dentro de 1. La muestra es CHICA
        # y el criterio se apoya en el manual; los 18 solo confirman que no lo contradice.
        aviso = espera_efectiva(pedido["created_at"], derivacion["created_at"])
        if aviso is not None and aviso <= RESPUESTA_TOPE:
            return Retiro(
                4, "buena",
                f"El retiro le correspondía a su agente y se lo informó en {_mins(aviso)}, "
                "con el número para contactarlo.",
                aviso, None, algo_mas, True)
        return Retiro(
            3, "aceptable",
            "El retiro le correspondía a su agente y se lo informó con el número, pero "
            f"tardó {_mins(aviso)} en decírselo. El objetivo son 5 minutos, que alcanzan "
            "para verificar a qué agencia pertenece.",
            aviso, None, algo_mas, True)
    if entrega is None:
        return Retiro(
            2, "deficiente",
            f"Respondió en {_mins(espera)}, pero nunca envió el comprobante del "
            "retiro: el agente no tiene con qué respaldar que la plata salió.",
            espera, None, algo_mas)
    if entrega > ENTREGA_TOPE or (espera is not None and espera > RESPUESTA_TOPE):
        return Retiro(
            2, "deficiente",
            f"Envió el comprobante, pero tarde: respondió en {_mins(espera)} y lo "
            f"entregó {_mins(entrega)} después del pedido.",
            espera, entrega, algo_mas)
    if (espera is not None and espera > AGIL) or entrega > ENTREGA_AGIL:
        return Retiro(
            3, "aceptable",
            f"Entregó el comprobante, pero fuera del objetivo: respondió en "
            f"{_mins(espera)} y entregó {_mins(entrega)} después del pedido. Se apunta "
            "a responder en 1 minuto y entregar dentro de los 15.",
            espera, entrega, algo_mas)
    if algo_mas and colgado:
        # Espejo de la rama de src/deposito.py: techo en 4 (ver tests/test_ultima_palabra.py).
        return Retiro(
            4, "buena",
            f"Respondió el pedido en {_mins(espera)} y entregó el comprobante "
            f"{_mins(entrega)} después, pero el cliente escribió al final y se quedó con "
            "la última palabra.",
            espera, entrega, True)
    if algo_mas:
        return Retiro(
            5, "excelente",
            f"Respondió el pedido en {_mins(espera)}, entregó el comprobante "
            f"{_mins(entrega)} después y antes de cerrar se aseguró de que no faltara "
            "nada.",
            espera, entrega, True)
    return Retiro(
        4, "buena",
        f"Respondió en {_mins(espera)} y entregó el comprobante {_mins(entrega)} "
        "después del pedido. Cerró sin preguntar si faltaba algo.",
        espera, entrega, False)


def _situacion(r: Retiro) -> str | None:
    """La RAMA que produjo la nota; el texto vive en src/catalogo_coaching.py."""
    if r.stars == 5:
        return None
    if r.stars == 1:
        return "1"
    if r.stars == 2:
        return "2_sin_comprobante" if r.entrega is None else "2_tarde"
    return str(r.stars)


def score_retiro(messages: list[dict], cierre_at=None, lineas=None) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM.

    None cuando no es una transaccion: una consulta sobre retiros se juzga por si el
    cliente entendio la respuesta, no por un comprobante que nunca correspondio.
    """
    r = calificar_retiro(messages, cierre_at, lineas)
    if r is None:
        return None
    _consejo = consejo_de("retiro", _situacion(r) or "")
    return ScoreResult(
        rubric="retiro",
        motivo="retiro",
        rating_label=r.label,
        stars=r.stars,
        rating_rationale=r.rationale,
        dimensions={
            "espera_respuesta_seg": (int(r.espera.total_seconds())
                                     if r.espera is not None else None),
            "entrega_comprobante_seg": (int(r.entrega.total_seconds())
                                        if r.entrega is not None else None),
            "pregunto_algo_mas": r.pregunto_algo_mas,
        },
        llm_model=MODELO_DETERMINISTA,
        # El eje de uplift de retencion se saco a proposito: medido, empujar en retiro
        # EMPEORA el deposito posterior (83,8% -> 69,9%). El cliente ya volvia solo.
        atencion=None,
        # None = NO OBSERVO, que es distinto de "observe que no hubo". `deposit_mismatch`
        # reconcilia el gate determinista contra la OBSERVACION del LLM; una rubrica
        # determinista no tiene opinion que reconciliar, y con `False` el flag comparaba el
        # gate contra un DEFAULT y disparaba al vacio. MEDIDO el 2026-08-12: 20 de los 48
        # mismatches de la corrida v6 eran estos. Igual que promo/info/soporte/agilidad.
        deposit_observed=None,
        floor_applied=False,
        # Del catalogo (src/catalogo_coaching.py): una sola fuente de verdad, y el
        # codigo viaja en la fila para poder CONTAR entre operadores.
        recomendacion=_consejo.texto if _consejo else "",
        recomendacion_codigos=[_consejo.codigo] if _consejo else [],
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
