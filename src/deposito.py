"""Rubrica DETERMINISTA del motivo `deposito`. Sin LLM y sin BD.

POR QUE DETERMINISTA. El motivo lo sigue clasificando el modelo — eso exige leer
intencion y es su trabajo irremplazable. La NOTA no: los tres hechos que la definen
son verificables contra los datos, no interpretables.

    el reloj          cuando llego el comprobante y cuando contesto el operador
    la acreditacion   dijo que la plata llego, no que la estaba procesando
    el cierre         chequeo que al cliente no le faltara nada

QUE PROBLEMA RESUELVE. Con la escala generica, `deposito` no calificaba: 86,4% de las
sesiones caian en 3 estrellas, y de 149 transacciones hechas PERFECTAS (respuesta
<=2 min + acreditacion confirmada) 135 quedaban en 3. Hacerlo bien valia +0,13
estrellas contra no hacerlo. Al sacar el cap de uplift la distribucion se dio vuelta
y el 47,5% de los depositos llegaba a 5 SOLO por cortesia — los operadores usan
plantillas calidas por defecto, asi que ser amable era gratis. Ninguna de las dos
escalas medía el trabajo.

LA ESCALA (definida por el negocio el 2026-08-06: "con que se haga bien y rapido es
suficiente"; el comprobante se exige por AUDITORIA y proteccion de la confianza, no
como metrica de satisfaccion del cliente):

    5  acuse <=1 min + confirmo la acreditacion + se aseguro de que no faltara nada
    4  acuse <=1 min + confirmo la acreditacion
    3  confirmo, pero el acuse tardo 1-5 min
    2  el acuse tardo >5 min, o nunca confirmo la acreditacion
    1  ni respondio ni confirmo

UMBRALES, calibrados sobre 1.254 transacciones (1 sesion por persona, jul-ago 2026):
el 78,0% acusa en <=2 min del comprobante y el 76,2% confirma en <=5 min. Los cortes
separan sin ser ni regalados ni imposibles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from src.deposits import has_recharge_context
from src.interacciones import interaccion_de
from src.operators import inicio_del_reloj
from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    operator_acreditacion,
    operator_acuso_comprobante,
    cliente_tuvo_la_ultima_palabra,
    operator_asked_and_waited,
    operador_derivo_al_agente,
    tiene_reloj,
)
# La espera se mide en HORARIO DE ATENCION (ver src/horario.py): 26 por ciento de los
# deficientes eran clientes que escribieron de madrugada y operadores que contestaron
# ni bien abrio el turno. La noche no es una demora del operador.
from src.horario import espera_efectiva

# Sentinela para la columna `llm_model`: no hubo modelo en la nota. Permite separar
# por SQL las filas del path determinista de las del pase con LLM.
MODELO_DETERMINISTA = "determinista/deposito-v1"

# EL RECHAZO VALIDO: la plata NO podia entrar y no es culpa del operador. Cuando eso pasa su
# trabajo es DECIRLO, y por eso hay una rama propia (ver calificar_deposito).
# Solo formas INEQUIVOCAS, y se mira despues del comprobante y sin acreditacion — el contexto
# desambigua "debe verificar", que suelto aparece 4.026 veces como instruccion general.
# DOS FALSOS POSITIVOS medidos que quedan AFUERA a proposito:
#   "Monto minimo: $5" -> 20.489 mensajes, es la PLANTILLA de como transferir;
#   "El bono esta vigente" -> "vigente" en contexto positivo.
# LIMITACION CONOCIDA: si el texto del rechazo trae vocabulario de acreditacion ("esa boleta
# ya fue CARGADA antes"), `operator_acreditacion` gana y la sesion no entra a esta rama.
_RECHAZO_RE = re.compile(
    r"titular (incorrecto|no coincide|distinto)"
    r"|(comprobante|boleta) (repetid|duplicad)"
    r"|\b(rechazad|denegad)"
    r"|no (se )?(puede|pudo|podr[aá]) (cargar|acreditar|procesar)"
    r"|(debe|debes|necesitas|tiene que|tienes que)[^.!?\n]{0,25}verificar",
    re.IGNORECASE)

AGIL = timedelta(minutes=1)       # <= 1 min -> el acuse fue inmediato
ACEPTABLE = timedelta(minutes=5)  # <= 5 min -> tolerable; mas que eso, no


@dataclass(frozen=True)
class Deposito:
    """Nota determinista de una sesion de deposito."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None      # del comprobante a la primera respuesta del operador
    acredito: bool
    pregunto_algo_mas: bool
    derivo_al_agente: bool = False  # la recarga no le correspondia a ATC (ver la rama)


# EL CONSEJO APUNTA A LA RAMA, NO A LA ESTRELLA. Al 2 se llega por DOS caminos —nunca
# confirmo la acreditacion, o la confirmo pero el acuse tardo— y un solo texto por estrella
# le decia al operador que no hizo algo que SI habia hecho, con su propio rationale al lado
# diciendo lo contrario. Medido el 2026-08-11: 370 de las 1.400 sesiones en 2 estrellas
# (26,4%) ya tenian `acredito=true` y recibian igual el consejo de "confirmale siempre".
_COACHING = {
    3: "Un primer mensaje corto —\"ya lo recibí, lo reviso\"— apenas entra el comprobante "
       "alcanza para que el cliente no quede en silencio mientras se procesa.",
    4: "Cerrar con \"¿te falta algo más?\" abre la puerta a la segunda duda, que en recargas "
       "suele ser el bono o el próximo depósito. Y conviene dar unos 5 minutos antes de "
       "cerrar el ticket: preguntar y cerrar en el mismo acto no deja tiempo de contestar.",
}
_COACHING_2_SIN_ACREDITAR = (
    "Conviene confirmar que la plata entró con una línea al cierre: \"listo, ya tienes "
    "tu saldo\". Un \"en breve\" deja esa pregunta sin responder, y el cierre con /FIN "
    "recién corresponde cuando la gestión terminó.")
_COACHING_2_TARDE = (
    "El primer aviso tardó demasiado. El manual separa los dos momentos y les da una "
    "respuesta rápida a cada uno: /R2verificaciondeboleta apenas entra el comprobante, y "
    "/R3Recarga cuando la carga ya está en curso.")
# LA RAMA DEL RECHAZO (ver calificar_deposito): la plata no podia entrar y el operador lo
# aviso. El consejo NO puede ser el del 4/3 normal, que habla del bono o del acuse.
_COACHING_RECHAZO_RAPIDO = (
    "El aviso salió rápido. Lo que más ayuda es decirle también cómo arreglarlo —qué dato "
    "corregir o cómo verificar la cuenta— para que el próximo intento sí entre.")
_COACHING_RECHAZO_TARDE = (
    "El rechazo conviene avisarlo enseguida: mientras espera, el cliente cree que su plata "
    "está en camino. Un mensaje corto en cuanto se ve el problema evita esa espera a ciegas.")
_COACHING_1 = ("El comprobante quedó sin respuesta. En caja conviene contestar siempre: una "
               "línea mientras se procesa evita que el cliente crea que se perdió su plata.")
# LA RAMA DE LA DERIVACION: el jugador es de un agente y ATC tiene PROHIBIDO recargarle
# (manual, cap. 06). El consejo no puede pedir la acreditacion -- pediria justo lo prohibido.
_COACHING_DERIVACION_RAPIDA = (
    "La derivación salió rápido. Suma indicarle también que puede recargar desde la "
    "plataforma, así tiene la opción a mano si no ubica a su agente.")
_COACHING_DERIVACION_TARDE = (
    "Cuando la recarga le corresponde al agente, conviene decirlo enseguida y pasar su "
    "número: mientras espera, el cliente cree que su plata ya está en camino.")


def _comprobantes_del_cliente(messages: list[dict]) -> list[dict]:
    """Comprobantes (imagenes del CLIENTE) en orden cronologico."""
    return [m for m in sorted((m for m in messages if not m.get("is_note")),
                              key=lambda m: m["created_at"])
            if not m.get("from_me") and is_real_media(m.get("media_type"))]


def _comprobante_del_cliente(messages: list[dict]):
    """El ULTIMO comprobante de la sesion: el que elige la interaccion a juzgar. None si no hay.
# EL ANCLA ELIGE LA ULTIMA VISITA. Antes tomaba la PRIMERA y una sesion mergea todos los
    # episodios del ticket: MEDIDO el 2026-08-12 sobre 1.180 sesiones con 2+ interacciones
    # calificables, la primera y la ultima estan separadas por una mediana de 8,6 h, un p90 de
    # 285 h (12 dias) y un maximo de 266 dias. Juzgar la primera es describir la visita mas vieja.
    # Era ademas el SEGUNDO CRITERIO MAS DURO de los seis medidos (3,42 estrellas contra 3,55 del
    # ultimo; 620 sesiones en 2 o menos contra 499).
    # Y lo decisivo: el 82% de esas sesiones tienen MAS DE UN OPERADOR (hasta 10). Con la primera,
    # la nota se le cargaba al que atendio la visita vieja -- cambiar a la ultima reatribuye 494 de
    # las 600 notas que se mueven. Por eso tampoco se PROMEDIA entre interacciones: seria mezclar
    # el trabajo de dos personas y ponerselo a una sola.
    """
    # EL ANCLA TIENE QUE SER UN COMPROBANTE, NO LA ULTIMA IMAGEN QUE PASO. `es_transaccion`
    # exige contexto de recarga pero lo mide sobre la SESION ENTERA, y la eleccion es de
    # INTERACCION: alcanzaba con que hubiera habido un deposito real mas atras para que
    # cualquier imagen posterior quedara habilitada como ancla, la ventana saltara a esa
    # visita, y la rubrica preguntara "¿confirmo la acreditacion?" sobre una conversacion sin
    # ningun deposito.
    # TRES CASOS REALES (auditoria del 2026-08-13), los tres con 2 estrellas y el rationale
    # "nunca le confirmo que la plata habia entrado":
    #   `0a61513b`  el ancla eligio una imagen con caption VACIO cuya interaccion es
    #               "Buenos días / ¿Hay algún problema con la página?" -- un problema de login.
    #   `23ff3128`  100 imagenes candidatas; eligio "Buenos días ING presente en la finca
    #               MARÍA MARÍA" -- una foto de una finca.
    #   `1f53cdc6`  dos recargas confirmadas por OTROS dos operadores y, seis dias despues, una
    #               imagen sobre apuestas: el 2 estrellas cayo sobre quien no tuvo deposito.
    # Se corrobora con LAS MISMAS DOS PUERTAS que ya usa `es_transaccion`, pero acotadas a la
    # interaccion de cada imagen. Si ninguna se corrobora no hay transaccion, y la sesion cede
    # el turno al pase con LLM -- lo mismo que ya pasa cuando no hay comprobante.
    for m in reversed(_comprobantes_del_cliente(messages)):
        visita = interaccion_de(messages, m)
        if (has_recharge_context(visita)
                or operator_acuso_comprobante(visita, desde=m["created_at"])):
            return m
    return None


def es_transaccion(messages: list[dict]) -> bool:
    """La sesion es un deposito HECHO, no una consulta sobre depositos.

    El COMPROBANTE del cliente es condicion necesaria: sin el no hay nada que acreditar.
    Medido sobre 3.539 sesiones con contexto de recarga (1 por persona): solo el 35,4%
    son transacciones; el 64,6% restante pregunta sin depositar y el 99,7% de esas no
    tiene nada que acreditar. Calificar una consulta con la vara transaccional castiga
    al operador por algo que nunca ocurrio.

    La RAZON de recarga puede venir por dos puertas, porque el cliente muchas veces no
    escribe nada (auditoria del 2026-08-11: el caption de 33.914 comprobantes es vacio y
    el de otros 11.270 lo pone la app del banco):
      1. la escribe el CLIENTE -> has_recharge_context (el criterio historico);
      2. no escribe nada y la corrobora el OPERADOR acusando el comprobante recibido
         -> operator_acuso_comprobante, exigido POSTERIOR al comprobante.
    Sin la puerta 2, 5.521 depositos con comprobante (99,96% de los que caian al pase con
    LLM) se calificaban sin reloj y sin chequear la acreditacion, donde ademas no hay
    techo: sacaban 5 estrellas el 68,2% de las veces contra el 3,6% de las transacciones
    medidas por esta rubrica.
    """
    if not tiene_reloj(messages):
        return False
    comprobante = _comprobante_del_cliente(messages)
    if comprobante is None:
        return False
    return (has_recharge_context(messages)
            or operator_acuso_comprobante(messages, desde=comprobante["created_at"]))


def interaccion_juzgada(messages: list[dict]) -> list[dict] | None:
    """La ventana que `calificar_deposito` va a juzgar. None si no es una transaccion.

    Espejo de `retiro.interaccion_juzgada` — el ancla aca es el COMPROBANTE del cliente.
    Existe para que los tiempos y el operador persistidos describan la interaccion juzgada
    y no la conversacion entera (ver src/interacciones.tiempos_de).
    """
    comprobante = _comprobante_del_cliente(messages) if es_transaccion(messages) else None
    return None if comprobante is None else interaccion_de(messages, comprobante)


def calificar_deposito(messages: list[dict], cierre_at=None, lineas=None) -> Deposito | None:
    """Nota determinista de la sesion. None si no es una transaccion de deposito.

    `lineas`: mapa de nuestras lineas (src/redireccion.build_lineas_map), para reconocer la
    derivacion al agente del cliente. Sin el mapa esa rama no se activa: falla del lado
    seguro, igual que `redireccion`."""
    if not es_transaccion(messages):
        return None
    comprobante = _comprobante_del_cliente(messages)
    # LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL COMPROBANTE, no en toda la sesion. La
    # frontera la pone el objeto: la nota de cierre del operador (ver src/interacciones.py).
    # Sin esto, en las 5.624 conversaciones con varios cierres -- el 3,51%, pero donde viven
    # el 41,7% de los mensajes -- la acreditacion de una transaccion acreditaba el
    # comprobante de otra. Caso `f9b31f4f`: 8 dias, 16 cierres y cuatro operadores en una
    # sola conversacion; un comprobante del 3-ago que nadie contesto salia como "confirmo la
    # acreditacion, pero tardo 39,5 horas en avisarle".
    ventana = interaccion_de(messages, comprobante)
    reales = sorted((m for m in ventana if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    # DENTRO de la ventana el reloj arranca en el PRIMER comprobante de ESA visita. El ancla
    # elige la interaccion (la ultima); el reloj mide la espera completa. Si el cliente manda
    # tres imagenes seguidas, contar desde la ultima esconderia la demora.
    comprobante = _comprobantes_del_cliente(reales)[0]
    # El reloj arranca en el COMPROBANTE, no en el primer mensaje: la charla previa
    # no es tiempo que el operador le deba al cliente.
    respuesta = next(
        (m for m in reales
         if _is_operator(m) and m["created_at"] > comprobante["created_at"]), None)
    # EL RELOJ ARRANCA CUANDO EL OPERADOR PUEDE RESPONDER, o sea en el mas TARDIO entre el
    # comprobante y la ENTREGA del ticket. Es la misma idea que ya rige en `espera_efectiva`,
    # que descuenta el horario: no se cobra lo que el operador no controla.
    # MEDIDO el 2026-08-13 sobre las 6 filas en 2 estrellas por "tardo en avisarle": en 5 de 6
    # el reloj era casi todo COLA. Un caso: 308,7 minutos de reloj, 300,2 de cola y **8,5 de
    # reaccion real**. Cuatro de esas cinco son de la misma operadora, que contesto entre 1,4 y
    # 8,5 minutos y cobro 2 estrellas por "tardar" -- con el umbral en 5 minutos, la cola sola
    # ya se los comia. El eje ya estaba medido desde el 2026-08-06 ("primer mensaje tras la
    # asignacion sirve como eje, deposito 0,7 min de mediana") y no se habia usado aca.
    # SIN NOTA DE ENTREGA NO SE DESCUENTA NADA: no se inventa una cola que no se puede probar.
    # Se busca en la VENTANA y desde el comprobante: una entrega anterior significa que el
    # operador YA tenia la conversacion, y ahi la demora es entera suya.
    inicio = inicio_del_reloj(ventana, comprobante["created_at"])
    espera = (espera_efectiva(inicio, max(respuesta["created_at"], inicio))
              if respuesta else None)
    acredito = operator_acreditacion(reales)
    algo_mas = operator_asked_and_waited(reales, cierre_at)
    colgado = cliente_tuvo_la_ultima_palabra(reales, cierre_at)
    # Se busca en la VENTANA, no en la sesion: la derivacion tiene que pertenecer a la misma
    # interaccion que el comprobante que se esta juzgando.
    derivacion = operador_derivo_al_agente(reales, lineas)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None and not acredito:
        return Deposito(1, "mala", "El cliente mandó el comprobante y nadie le respondió.",
                        None, False, algo_mas)
    if not acredito and derivacion is not None:
        # LA RAMA DE LA DERIVACION AL AGENTE. Manual de ATC cap. 06: "Si un jugador pertenece
        # a un agente, el operador NO debe realizar recargas ni retiros". Exigirle la
        # acreditacion es exigirle el paso PROHIBIDO. MEDIDO el 2026-08-19: 152 sesiones de
        # `deposito` con derivacion, media 3,08 estrellas y 70 en 1-2 (46%); 3 de 3
        # transcripts leidos son el procedimiento correcto castigado (`009312d9`, `03566bc9`,
        # `09c1b759`).
        # SE CALIFICA LA VELOCIDAD DEL AVISO, igual que la rama del rechazo de aca abajo y
        # que la del alta imposible en src/registro.py, y con el mismo TECHO EN 4: el 5 es "el
        # mejor escenario del motivo", y una recarga que ATC no podia hacer no lo es.
        # VA ANTES DEL RECHAZO GENERICO porque es la razon mas especifica: un mismo mensaje
        # puede sonar a rechazo y ser una derivacion, y el consejo de cada rama es distinto.
        # NO APLICA SI ACREDITO: ahi corre la excepcion del manual ("si el jugador expresa que
        # desea que le ayudemos con la recarga podemos proceder") y la nota normal ya es justa.
        # EL RELOJ DE ESTA RAMA NO ES `AGIL`, y la razon es del manual, no de los datos: antes
        # de derivar, el operador TIENE que pedir el usuario y verificar en el sistema a que
        # agencia pertenece (cap. 05, "Solicitud directa de cuenta bancaria", pasos 1 y 2).
        # Eso es una CONSULTA, no un reflejo, y el minuto de `AGIL` mide la primera respuesta.
        # Cobrarle el minuto seria cobrarle la verificacion que el manual le exige hacer.
        # Se usa el mismo tope que la rama del alta imposible de src/registro.py (5 min para
        # un aviso que requiere mirar el sistema). MEDIDO sobre los 18 casos que la señal
        # encuentra: p50 4,3 min, 56% dentro de 5 y solo 11% dentro de 1. La muestra es CHICA
        # y el criterio se apoya en el manual; los 18 solo confirman que no lo contradice.
        aviso = espera_efectiva(comprobante["created_at"], derivacion["created_at"])
        if aviso is not None and aviso <= ACEPTABLE:
            return Deposito(
                4, "buena",
                f"La recarga le correspondía a su agente y se lo informó en {_mins(aviso)}, "
                "con el número para contactarlo.",
                aviso, False, algo_mas, True)
        return Deposito(
            3, "aceptable",
            "La recarga le correspondía a su agente y se lo informó con el número, pero "
            f"tardó {_mins(aviso)} en decírselo. El objetivo son 5 minutos, que alcanzan "
            "para verificar a qué agencia pertenece.",
            aviso, False, algo_mas, True)
    if not acredito:
        # LA RAMA DEL RECHAZO. Si la plata no podia entrar por una razon valida (titular
        # incorrecto, boleta repetida, cuenta sin verificar), el trabajo del operador es
        # AVISARLO: se califica por la velocidad de ese aviso, con TECHO EN 4. El 5 no es
        # alcanzable aca a proposito -- significa "el mejor escenario del motivo", y un
        # deposito rechazado no lo es. Decision del negocio, 2026-08-12.
        rechazo = next((m for m in reales
                        if _is_operator(m) and m["created_at"] > comprobante["created_at"]
                        and _RECHAZO_RE.search(m.get("body") or "")), None)
        if rechazo is not None:
            aviso = espera_efectiva(comprobante["created_at"], rechazo["created_at"])
            if aviso <= AGIL:
                return Deposito(
                    4, "buena",
                    f"La recarga no se pudo acreditar y se lo informó en {_mins(aviso)}. "
                    "El cliente supo enseguida por qué y qué le faltaba.",
                    aviso, False, algo_mas)
            return Deposito(
                3, "aceptable",
                f"La recarga no se pudo acreditar y se lo informó, pero tardó "
                f"{_mins(aviso)} en decírselo. El objetivo es 1 minuto.",
                aviso, False, algo_mas)
        return Deposito(
            2, "deficiente",
            f"Respondió en {_mins(espera)}, pero nunca le confirmó al cliente que la "
            "plata había entrado: se quedó sin saber si su recarga se acreditó.",
            espera, False, algo_mas)
    if espera is None or espera > ACEPTABLE:
        return Deposito(
            2, "deficiente",
            f"Confirmó la acreditación, pero tardó {_mins(espera)} en avisarle al "
            "cliente que había recibido el comprobante.",
            espera, True, algo_mas)
    if espera > AGIL:
        return Deposito(
            3, "aceptable",
            f"Confirmó la acreditación, pero tardó {_mins(espera)} en el primer aviso. "
            "El objetivo es 1 minuto.",
            espera, True, algo_mas)
    if algo_mas and colgado:
        # PREGUNTO Y SE FUE. El 5 dice, literal, "antes de cerrar se aseguró de que no le
        # faltara nada": no se puede afirmar de una sesion donde el cliente contesto esa
        # pregunta y nadie le respondio. Es un TECHO, no un castigo -- el trabajo se hizo y
        # la nota lo refleja en el 4. Ver tests/test_ultima_palabra.py.
        return Deposito(
            4, "buena",
            f"Avisó en {_mins(espera)} y le confirmó al cliente que la plata entró, pero "
            "el cliente escribió después y se quedó con la última palabra.",
            espera, True, True)
    if algo_mas:
        return Deposito(
            5, "excelente",
            f"Avisó en {_mins(espera)} que había recibido el comprobante, le confirmó "
            "al cliente que la plata entró y antes de cerrar se aseguró de que no le "
            "faltara nada.",
            espera, True, True)
    return Deposito(
        4, "buena",
        f"Avisó en {_mins(espera)} que había recibido el comprobante y le confirmó al "
        "cliente que la plata entró. Cerró sin preguntarle si necesitaba algo más.",
        espera, True, False)


def _coaching(d: Deposito) -> str:
    """El consejo de la RAMA que produjo la nota (ver la nota de `_COACHING`)."""
    if d.stars == 5:
        return ""
    if d.stars == 1:
        return _COACHING_1
    if d.stars == 2:
        return _COACHING_2_SIN_ACREDITAR if not d.acredito else _COACHING_2_TARDE
    # 4 o 3 SIN acreditacion = la rama del rechazo (en la normal el 4 y el 3 siempre
    # acreditaron). Su consejo apunta al rechazo, no al bono ni al acuse.
    if d.derivo_al_agente:
        return (_COACHING_DERIVACION_RAPIDA if d.stars == 4
                else _COACHING_DERIVACION_TARDE)
    if not d.acredito:
        return _COACHING_RECHAZO_RAPIDO if d.stars == 4 else _COACHING_RECHAZO_TARDE
    return _COACHING[d.stars]


def score_deposito(messages: list[dict], cierre_at=None, lineas=None) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM.

    None cuando la sesion no es una transaccion de deposito: ahi decide el caller
    (hoy, el pase con LLM), porque una consulta sobre recargas se juzga por si el
    cliente entendio la respuesta, no por un comprobante que nunca existio.
    """
    d = calificar_deposito(messages, cierre_at, lineas)
    if d is None:
        return None
    return ScoreResult(
        rubric="deposito",
        motivo="deposito",
        rating_label=d.label,
        stars=d.stars,
        rating_rationale=d.rationale,
        dimensions={
            "espera_acuse_seg": (int(d.espera.total_seconds())
                                 if d.espera is not None else None),
            "acredito": d.acredito,
            "pregunto_algo_mas": d.pregunto_algo_mas,
        },
        llm_model=MODELO_DETERMINISTA,
        # `atencion` es la vara COMERCIAL del jugador (empujar registro/deposito). En
        # deposito el eje comercial se saco a proposito: el cliente ya deposito.
        atencion=None,
        deposit_observed=True,
        floor_applied=False,
        # La recomendacion es EXACTAMENTE el gap hacia el 5. En el mejor escenario
        # queda vacia: no hay nada que corregir.
        recomendacion=_coaching(d),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
