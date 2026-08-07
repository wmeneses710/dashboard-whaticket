"""Rubrica DETERMINISTA de agilidad para el segmento AGENTE (sin LLM).

POR QUE NO HAY LLM ACA. El agente es un vendedor/afiliador que opera una caja: pide una
carga o descarga y el operador la ejecuta. Son operaciones de RUTINA que no requieren
validacion ni criterio, asi que la calidad de la atencion es UNA sola cosa medible con
timestamps: cuanto tardo el operador en responder. Definido por el negocio (2026-08-05):
rapido = excelente, se demora = baja, lo abandona = 1 estrella. Nada de eso necesita un
modelo, y hacerlo determinista lo vuelve barato, reproducible y auditable.

La vara comercial del jugador (uplift = empujar registro/deposito, eje `atencion` =
empujo/pasivo) NO aplica a un revendedor profesional: es la deuda que este modulo paga
(ver el docstring de src/segments.py).

UNIDAD DE MEDIDA: el TURNO. Un turno es un BLOQUE de mensajes consecutivos del agente y
la primera respuesta del operador posterior. La espera se mide desde el PRIMER mensaje
del bloque, que es la que percibe el agente.

TRES CONFOUNDS, medidos sobre whaticket_copia (60.447 sesiones de agente, 135.969
turnos). Sin excluirlos la rubrica castiga al operador por cosas que no controla:

1. HORARIO. La operacion corre 06:00-23:59 Ecuador. Mediana de espera por hora: 00h =
   20.869s (5,8 h), 03h = 8.827s, 05h = 1.734s, y a las 06h cae a 54s y se queda entre
   43 y 66s durante 18 horas. La espera de madrugada es el tiempo hasta que entra el
   turno, no lentitud. Son el 0,5% del volumen pero inflaban la media de 57s a 660s.

2. CORTESIA. El peor turno de una sesion suele ser un "Ok" o un "Gracias", que no piden
   nada. Sin este filtro la sesion cae a 3 estrellas por no correr a contestar un
   agradecimiento.

3. OPERACION YA CERRADA. De 3.283 comprobantes sin respuesta, 1.654 (50,4%) tenian ya la
   confirmacion del operador ANTES: la transaccion estaba hecha y la imagen extra no
   exige respuesta. Solo la otra mitad es abandono real.

OJO CON LA MEDIA: p50 = 57s pero la media es 660s (11x). Los umbrales salen de
percentiles, no de promedios. Con estas bandas la distribucion medida fue 60,8% / 21,4% /
10,8% / 4,2% / 2,8% (5 a 1 estrella).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.rubrics import formato_espera, plural
from src.scorer import ScoreResult
from src.signals import (
    es_cortesia,
    is_real_media,
    operator_confirmation,
    operator_sent_media,
)
# El horario vive en src/horario.py (fuente unica); se reexporta para no romper
# los imports existentes de agilidad.
from src.horario import HORA_ABRE, HORA_CIERRA, espera_efectiva

# Sentinela que va en la columna `llm_model`. No hubo modelo: sirve para poder separar
# por SQL las filas del path determinista de las del pase con LLM.
MODELO_DETERMINISTA = "determinista/agilidad-v1"

# Bandas de espera del PEOR pedido de la sesion -> etiqueta de la escala v2.
AGIL = timedelta(minutes=2)        # <= 2 min  -> excelente (5)
BUENO = timedelta(minutes=5)       # <= 5 min  -> buena     (4)
ACEPTABLE = timedelta(minutes=15)  # <= 15 min -> aceptable (3)
#                                    > 15 min -> deficiente (2)
#                    pedido abandonado sin confirmar nada -> mala      (1)

# Horario de operacion (inclusive), en hora local de Ecuador. Ver confound 1.
TZ = ZoneInfo("America/Guayaquil")


@dataclass(frozen=True)
class Turno:
    """Un bloque de pedidos del agente y la primera respuesta del operador."""
    pedido_at: datetime
    respuesta_at: datetime | None
    es_pedido: bool     # el bloque exige respuesta (ver es_pedido)
    en_horario: bool    # el pedido entro dentro del horario de operacion

    @property
    def espera(self) -> timedelta | None:
        """Cuanto tardo el operador. None si nunca respondio."""
        if self.respuesta_at is None:
            return None
        return self.respuesta_at - self.pedido_at


@dataclass(frozen=True)
class Agilidad:
    """Nota determinista de agilidad de una sesion de agente.

    stars/label en None = la sesion no tiene NINGUN pedido en horario, asi que no hay
    agilidad que medir (p. ej. solo cortesias, o todo de madrugada). No es un 0: es
    ausencia de material para juzgar, y el caller decide que hacer.
    """
    stars: int | None
    label: str | None
    rationale: str
    turnos_pedido: int
    peor_espera: timedelta | None
    sin_respuesta: int          # pedidos abandonados que SI cuentan como falla


def _es_real(m: dict) -> bool:
    """Mensaje que cuenta: no es nota interna."""
    return not m.get("is_note")


def es_pedido(bloque: list[dict]) -> bool:
    """El bloque de mensajes del agente EXIGE una respuesta del operador?

    Un comprobante (media REAL) SIEMPRE es pedido: hay que confirmar la acreditacion. Si
    no hay media, se juzga el texto COMPLETO del bloque: pura cortesia, acuse o saludo no
    pide nada; vacio tampoco.

    El chequeo de media va por `is_real_media` y no por truthiness de `media_type`: un
    texto normal de WhatsApp llega con media_type='chat', que es truthy, asi que el
    atajo de arriba se llevaba puesto TODO bloque no vacio y el `_CORTESIA_RE` de abajo
    no se ejecutaba nunca. Medido en produccion: 2 de los 4 veredictos de 1 estrella
    eran falsos — el "pedido sin responder" era la palabra "Gracias" en un caso y "Ok"
    en el otro, con el operador habiendo contestado la consulta real en 52 y 61 segundos.
    """
    if any(is_real_media(m.get("media_type")) for m in bloque if _es_real(m)):
        return True
    texto = " ".join(
        " ".join((m.get("body") or "").split()) for m in bloque if _es_real(m)
    ).strip()
    if not texto:
        return False
    return not es_cortesia(texto)


def bloques_del_cliente(messages: list[dict]) -> list[list[dict]]:
    """Parte el transcript en BLOQUES de mensajes consecutivos del agente.

    Ignora notas internas: una nota del operador no interrumpe el bloque del agente ni
    cuenta como respuesta.
    """
    bloques: list[list[dict]] = []
    actual: list[dict] = []
    for m in sorted((m for m in messages if _es_real(m)), key=lambda m: m["created_at"]):
        if m.get("from_me"):
            if actual:
                bloques.append(actual)
                actual = []
        else:
            actual.append(m)
    if actual:
        bloques.append(actual)
    return bloques


def _en_horario(cuando: datetime) -> bool:
    """El pedido entro dentro del horario de operacion (hora local de Ecuador)."""
    return HORA_ABRE <= cuando.astimezone(TZ).hour <= HORA_CIERRA


def turnos_de_agilidad(messages: list[dict]) -> list[Turno]:
    """Un Turno por bloque del agente, con la primera respuesta del operador posterior."""
    reales = sorted((m for m in messages if _es_real(m)), key=lambda m: m["created_at"])
    respuestas = [m["created_at"] for m in reales if m.get("from_me")]
    out = []
    for bloque in bloques_del_cliente(messages):
        inicio = bloque[0]["created_at"]
        fin_bloque = bloque[-1]["created_at"]
        respuesta = next((r for r in respuestas if r > fin_bloque), None)
        out.append(Turno(
            pedido_at=inicio,
            respuesta_at=respuesta,
            es_pedido=es_pedido(bloque),
            en_horario=_en_horario(inicio),
        ))
    return out


def _label_de(peor: timedelta) -> tuple[int, str]:
    if peor <= AGIL:
        return 5, "excelente"
    if peor <= BUENO:
        return 4, "buena"
    if peor <= ACEPTABLE:
        return 3, "aceptable"
    return 2, "deficiente"


def _seg(td: timedelta) -> int:
    return int(td.total_seconds())


def calificar_agilidad(messages: list[dict]) -> Agilidad:
    """Nota determinista de agilidad de una sesion de agente. PURA, sin LLM ni BD.

    Manda el PEOR pedido, no el promedio: el negocio pidio que sea rapido SIEMPRE, y son
    operaciones de rutina. (Con la mediana en vez del peor, la distribucion medida daba
    78,3% de excelentes en vez de 60,8%.)

    Un pedido ABANDONADO baja a 'mala' (1), pero solo si el operador no confirmo nada en
    la sesion: si ya habia confirmado, el comprobante extra no exige respuesta y no es
    abandono (confound 3).
    """
    pedidos = [t for t in turnos_de_agilidad(messages) if t.es_pedido and t.en_horario]
    if not pedidos:
        return Agilidad(stars=None, label=None, turnos_pedido=0, peor_espera=None,
                        sin_respuesta=0,
                        rationale="El agente no pidió nada dentro del horario de "
                                  "atención, así que no hay tiempo de respuesta que medir.")

    # El operador cumple de DOS formas y las dos valen: diciendolo ("acreditado") o
    # mandando el COMPROBANTE. El chequeo era solo de texto, asi que un retiro resuelto
    # con la imagen del comprobante y sin una sola palabra contaba como abandono y se
    # llevaba el 1 estrella (caso real 5177aa96). `operator_sent_media` ya existe para
    # exactamente esto y filtra bots y notas.
    ya_confirmo = operator_confirmation(messages) or operator_sent_media(messages)
    abandonados = [t for t in pedidos if t.respuesta_at is None] if not ya_confirmo else []
    esperas = [t.espera for t in pedidos if t.espera is not None]
    peor = max(esperas) if esperas else None

    if abandonados:
        return Agilidad(
            stars=1, label="mala", turnos_pedido=len(pedidos), peor_espera=peor,
            sin_respuesta=len(abandonados),
            rationale=f"{plural(len(abandonados), 'pedido')} del agente "
                      f"{'quedó' if len(abandonados) == 1 else 'quedaron'} sin respuesta, "
                      "y en toda la conversación el operador tampoco confirmó la operación "
                      "ni envió el comprobante.",
        )
    if peor is None:
        # Todos los pedidos quedaron sin respuesta PERO el operador ya habia confirmado:
        # la operacion estaba cerrada, no hay espera que medir ni falla que imputar.
        return Agilidad(stars=None, label=None, turnos_pedido=len(pedidos),
                        peor_espera=None, sin_respuesta=0,
                        rationale="El operador ya había confirmado la operación, así que "
                                  "los mensajes que vinieron después no esperaban respuesta.")

    stars, label = _label_de(peor)
    return Agilidad(
        stars=stars, label=label, turnos_pedido=len(pedidos), peor_espera=peor,
        sin_respuesta=0,
        rationale=f"La espera más larga del agente fue de {formato_espera(_seg(peor))}, "
                  f"sobre {plural(len(pedidos), 'pedido')} dentro del horario de atención.",
    )


# Coaching determinista por banda. Solo se emite cuando hay algo que corregir: en
# 'excelente' queda vacio (igual que el pase con LLM, que no recomienda si no hay que
# mejorar nada). Es fijo a proposito: la accion correctiva de una operacion de rutina no
# depende del caso, depende del reloj.
_COACHING = {
    "mala": "Quedó un pedido sin responder. En operaciones de caja conviene contestar "
            "siempre, aunque sea con una línea avisando que ya se está procesando.",
    "deficiente": "La respuesta tardó más de 15 minutos. Son operaciones de rutina que no "
                  "necesitan verificación: se puede avisar enseguida y confirmar al acreditar.",
    "aceptable": "La respuesta tardó más de 5 minutos. Si no se puede procesar en el momento, "
                 "un mensaje corto alcanza para que el agente no quede esperando sin saber.",
    "buena": "Muy cerca del objetivo, que es responder dentro de los 2 minutos.",
}


def score_agilidad(messages: list[dict]) -> ScoreResult | None:
    """Nota de agilidad como ScoreResult, lista para build_score_record. SIN LLM.

    Devuelve None cuando la sesion no tiene ningun pedido en horario que medir: el caller
    decide (no se inventa una nota media, que seria un dato falso).

    QUE NO SE LLENA, y por que:
    - `motivo`: clasificar el motivo exige leer intencion, y eso es trabajo de modelo. Se
      deja en None antes que adivinarlo. CONSECUENCIA: las sesiones de agente no
      aparecen en los cuadros que agrupan por motivo.
    - `atencion` (empujo|pasivo|no_respondio): es la vara COMERCIAL del jugador (empujar
      registro/deposito). No aplica a un revendedor profesional; era justamente uno de
      los sesgos que este modulo viene a sacar.
    - `deposit_observed`: es la observacion del LLM. El gate determinista de deposito ya
      vive en su propia columna (`deposit_count`).
    """
    a = calificar_agilidad(messages)
    if a.stars is None:
        return None
    return ScoreResult(
        rubric="agilidad",
        motivo=None,
        dimensions={
            "peor_espera_seg": _seg(a.peor_espera) if a.peor_espera else None,
            "turnos_pedido": a.turnos_pedido,
            "sin_respuesta": a.sin_respuesta,
            "errores": [], "aciertos": [],
        },
        rating_label=a.label,
        rating_rationale=a.rationale,
        stars=a.stars,
        llm_model=MODELO_DETERMINISTA,
        atencion=None,
        deposit_observed=None,
        floor_applied=False,
        recomendacion=_COACHING.get(a.label, ""),
    )
