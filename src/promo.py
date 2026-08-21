"""Rubrica DETERMINISTA del motivo `promo`. Sin LLM y sin BD.

`promo` es el UNICO motivo donde el eje de UPLIFT sobrevive, y no por gusto: esta
probado con datos de negocio (2026-08-05). Con empuje + MATERIAL el deposito
posterior sube de 24,9% a 34,1% (+9,2 pp). Empujando SOLO CON PALABRAS baja a 19,1%,
**peor que no hacer nada**. Por eso el material no es un adorno, es la condicion.

Y ese es el problema que la rubrica viene a marcar: medido sobre 424 sesiones (1 por
persona), el **68,2% empuja pero solo el 11,8% manda material**. O sea que ~56% de las
sesiones de promo hacen exactamente la version contraproducente.

ESCALA:
    5  mando MATERIAL y respondio <=5 min   (el mejor escenario probado)
    4  respondio <=2 min, sin material
    3  respondio entre 2 y 15 min, sin material
    2  respondio despues de 15 min
    1  no respondio

EL MATERIAL VALE MAS QUE UN PAR DE MINUTOS: mandarlo a los 4 min es mejor que
contestar de palabra en 1. Es lo unico que la prueba de negocio mostro que mueve la
conversion.

POR QUE EL MATERIAL ES PALANCA DEL 5 Y NO CORTE DEL 4. Si el 4 exigiera material, el
88% de las sesiones caeria debajo y volveriamos a una escala aplastada — el mismo
defecto que el cap de uplift que acabamos de sacar. Como palanca del 5 deja un techo
exigente pero real (11,6% lo alcanza hoy), que es exactamente la conducta que el
negocio quiere multiplicar.

LOS RELOJES: mediana 1,7 min, 56,8% <=2 min, 12,5% entre 5 y 15, 4,7% arriba de 15.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _is_operator,
    is_real_media,
    operator_pushed,
    planteo_del_cliente,
    tiene_reloj,
)
# La espera se mide en HORARIO DE ATENCION (ver src/horario.py): 26 por ciento de los
# deficientes eran clientes que escribieron de madrugada y operadores que contestaron
# ni bien abrio el turno. La noche no es una demora del operador.
from src.horario import espera_efectiva
from src.operators import inicio_del_reloj

MODELO_DETERMINISTA = "determinista/promo-v1"

AGIL = timedelta(minutes=1)        # respuesta inmediata (el manual de ATC lo fija dos veces)
RAZONABLE = timedelta(minutes=5)   # hasta aca el material sigue llegando a tiempo
TOLERABLE = timedelta(minutes=15)  # mas que esto, la consulta ya se enfrio

_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class Promo:
    """Nota determinista de una sesion de promo."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None
    material: bool
    empuje: bool


_COACHING = {
    2: "Una consulta de promo se enfría rápido. Conviene responder aunque sea con lo que "
       "se sabe y completar después, en vez de esperar a tener todo el detalle.",
    3: "En promo la ventana es corta: un primer mensaje dentro del minuto —aunque "
       "sea \"ya te confirmo el detalle\"— evita que la consulta se enfríe.",
    # 4 ESTRELLAS SE QUEDO SIN CONSEJO (2026-08-21). Decia "Una imagen marcando donde
    # tocar, o un video corto, hace lo que el texto no puede: le muestra el camino". Eran
    # 6.300 recomendaciones y el manual NO lo respalda: prescribe video en solo dos
    # procedimientos -- el tutorial de actualizacion de numero en BackOffice y las
    # "solicitudes de videos personalizados" que pide un agente -- y en ninguno se trata de
    # explicar una promo.
    # LA HISTORIA IMPORTA Y SE CONSERVA: el texto NO era una invencion nuestra. La version
    # anterior decia "el flyer o el enlace" y ATC no entendia a que se referia (2026-08-11,
    # no usan ninguno de esos dos artefactos); "imagen o video" salio de ellos. Se retiro
    # igual, ya sabiendo esto: no tener respaldo escrito es distinto de estar inventado, y la
    # decision del negocio fue que un consejo que el manual no sostiene no se emite. Si
    # vuelve, vuelve con la cita.
    # NO SE REEMPLAZA POR UNO GENERICO: tapar el hueco con relleno es volver a inventar, y el
    # operador lee el coaching como politica de la empresa.
}
_COACHING_1 = ("El cliente preguntó por la promo y nadie le respondió. Es la consulta con "
               "más intención de todas: conviene contestar aunque sea con lo que se sabe.")


def _material_del_operador(messages: list[dict]) -> bool:
    """El operador le mando algo CONCRETO ademas del texto: un adjunto o un enlace.

    QUE MANDAN DE VERDAD. Auditado el 2026-08-11 leyendo los adjuntos: NO es material de
    promocion — ATC lo dijo y los datos lo confirman. Son CAPTURAS ANOTADAS que muestran donde
    tocar ("presionas donde te encerre", "asi debes seleccionar los 3 eventos", "presiona ahi y
    te lleva al juego donde estan tus giros") y videos de como hacerlo. Eso le da mecanismo al
    numero que no cerraba: material SIN empuje convierte 24,8% y con empuje 5,7%, porque
    mostrarle a alguien donde apretar sirve y pegarle un "depositá ya" no.

    SOLO SE AFIRMA LA FORMA, NUNCA EL CONTENIDO. De un adjunto conocemos el `media_type` y
    de una URL el dominio; no hay manera de saber si esa imagen es material de la promo o
    una foto cualquiera. Por eso ni este nombre ni los textos que lee el operador dicen
    "flyer": el equipo de atencion no entendia su propia retroalimentacion porque nombraba
    un artefacto que no usa (criterio del negocio, 2026-08-11). Lo que se afirma es lo
    unico verificable — que el cliente se llevo algo mas que la explicacion.
    """
    for m in messages:
        if not _is_operator(m):
            continue
        if is_real_media(m.get("media_type")):
            return True
        if _LINK_RE.search(m.get("body") or ""):
            return True
    return False


def calificar_promo(messages: list[dict]) -> Promo | None:
    """Nota determinista de la sesion. None si no hay reloj para medirla."""
    if not tiene_reloj(messages):
        return None
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    # EL RELOJ ARRANCA EN EL PLANTEO, NO EN EL SALUDO. Anclaba en el primer mensaje del
    # cliente igual que `info`, y la poblacion de `promo` es cinco veces mas grande: **658 de
    # las 10.163 sesiones deterministas (6,5%) abren con una cortesia y traen la consulta
    # despues** (medido el 2026-08-17 sobre la corrida v16). Caso `07b642b4`: la nota decia
    # 8,6 HORAS de espera y el operador contesto la consulta real **6 segundos** despues de
    # que llegara. Los dos guards que lo vuelven seguro viven en `signals.planteo_del_cliente`
    # -- en particular, el ancla NO se mueve a un mensaje que nadie contesto, porque si no el
    # arreglo fabrica 1 estrella ("nadie le respondió") sobre una despedida.
    primero_cliente = planteo_del_cliente(reales)
    if primero_cliente is None:
        return None
    respuesta = next(
        (m for m in reales
         if _is_operator(m) and m["created_at"] >= primero_cliente["created_at"]), None)
    # EL RELOJ ARRANCA CUANDO EL OPERADOR PUEDE RESPONDER (ver src/operators.inicio_del_reloj):
    # la espera EN COLA no es suya. v15 aplico esto a deposito/retiro/info y dejo a `promo`
    # afuera sin que ninguna decision lo registrara (a diferencia de `soporte`, cuya exclusion
    # SI esta documentada). MEDIDO el 2026-08-14: de 2.746 filas deterministas, 276 tienen
    # cola sin descontar y 185 de mas de 5 minutos -- proporcionalmente MAS que el motivo que
    # el arreglo vino a corregir. Caso `2603e73c`: 2 estrellas por "26,3 minutos", de los
    # cuales 14,6 eran cola.
    # Se le pasa `messages` y NO `reales`: el helper busca las NOTAS del CRM, y `reales` las
    # filtra. `promo` no acota ventana, asi que la sesion entera es el alcance correcto.
    inicio = inicio_del_reloj(messages, primero_cliente["created_at"])
    espera = (espera_efectiva(inicio, max(respuesta["created_at"], inicio))
              if respuesta else None)
    material = _material_del_operador(reales)
    empuje = operator_pushed(reales)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if respuesta is None:
        return Promo(1, "mala", "El cliente preguntó por la promo y nadie le respondió.",
                     None, material, empuje)
    if espera > TOLERABLE:
        return Promo(2, "deficiente",
                     f"Respondió recién {_mins(espera)} después. Una consulta por una "
                     "promo se enfría rápido.",
                     espera, material, empuje)
    # EL MATERIAL manda, y vale mas que un par de minutos: es lo unico que la prueba
    # de negocio mostro que mueve la conversion. `operator_pushed` NO entra en la
    # condicion: en este motivo es casi tautologico, porque su patron matchea
    # `bono|promo|giros gratis` y hablar de la promo ES el motivo. Por eso el empuje
    # verbal solo no distingue nada (68,2% lo hace) y el material si (11,8%).
    if material and espera <= RAZONABLE:
        return Promo(5, "excelente",
                     f"Respondió en {_mins(espera)} y no se lo explicó solo con texto: "
                     "le mostró cómo, que es lo que hace que la promo convierta.",
                     espera, True, empuje)
    if espera > AGIL:
        return Promo(3, "aceptable",
                     f"Respondió en {_mins(espera)} y solo con texto. Se apunta a "
                     "contestar en 1 minuto, o a mostrarle cómo con una captura.",
                     espera, material, empuje)
    return Promo(4, "buena",
                 f"Respondió en {_mins(espera)}, pero le explicó la promo solo con "
                 "texto: no le mostró cómo.",
                 espera, material, empuje)


def score_promo(messages: list[dict]) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM."""
    p = calificar_promo(messages)
    if p is None:
        return None
    return ScoreResult(
        rubric="promo",
        motivo="promo",
        rating_label=p.label,
        stars=p.stars,
        rating_rationale=p.rationale,
        dimensions={
            "espera_respuesta_seg": (int(p.espera.total_seconds())
                                     if p.espera is not None else None),
            "mando_material": p.material,
            "empujo": p.empuje,
        },
        llm_model=MODELO_DETERMINISTA,
        # Unico motivo donde `atencion` significa algo: el empuje ES el eje.
        atencion="empujo" if p.empuje else "pasivo",
        deposit_observed=None,
        floor_applied=False,
        recomendacion="" if p.stars == 5 else (
            # `.get` y no `[]`: desde que 4 estrellas se quedo sin consejo la clave puede
            # faltar, y un KeyError aca tiraria el scoring de la sesion entera.
            _COACHING_1 if p.stars == 1 else _COACHING.get(p.stars, "")),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
