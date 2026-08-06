"""Rubrica DETERMINISTA del motivo `registro`. Sin LLM y sin BD.

LA DEFINICION la cerro el negocio el 2026-08-06: `registro` es UNA sola cosa — el
cliente pasa sus datos y el operador le devuelve las credenciales. Eso convierte un
cliente potencial en jugador. Si ademas logro que depositara, es el mejor escenario
posible.

POR QUE HACIA FALTA. El tag del LLM tenia ~25% de precision: de 206 filas etiquetadas
`registro` solo 52 tenian credenciales entregadas, y perdia otras 29 que habian
quedado en `promo` o `soporte_cuenta`. La causa raiz medida: el modelo clasificaba lo
que OFRECIO EL OPERADOR (su plantilla de venta menciona crear la cuenta en casi toda
prospeccion) en vez de por que vino el CLIENTE. Esta rubrica no arregla el TAG — eso
sigue siendo trabajo del modelo — pero si la NOTA, que sale de hechos verificables.

ESCALA:
    5  entrego credenciales Y logro el deposito en la misma sesion
    4  entrego credenciales dentro de los 5 min del traspaso de datos
    3  entrego credenciales pero tardo mas de 5 min
    2  el cliente paso sus datos y NUNCA recibio credenciales (alta a medias)
    1  el cliente paso sus datos y no hubo ninguna respuesta

EL 5 CUENTA AUNQUE EL DEPOSITO VENGA ANTES. Decision del negocio: "cuenta por un tema
estadistico, algo de suerte es pero asi queda, tal vez fue un tema del operador
anterior pero no nos mataremos con eso". Son 3 de 108 casos medidos.

UMBRAL, calibrado sobre 707 registros (1 sesion por persona, jul-ago 2026): del
traspaso de datos a las credenciales la mediana es 3,1 min y el 69,1% entra en 5 min.
El corte de 2 min de deposito/retiro aca seria injusto — solo el 26,3% lo alcanza —
porque crear una cuenta lleva mas que acusar un comprobante.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from src.deposits import deposit_candidate_count
from src.scorer import ScoreResult
from src.signals import _is_operator, operator_sent_credentials, tiene_reloj

MODELO_DETERMINISTA = "determinista/registro-v1"

ENTREGA_AGIL = timedelta(minutes=5)   # del traspaso de datos a las credenciales

# Datos personales que el cliente manda para que le creen la cuenta. El correo y la
# cedula son los dos campos del formulario que no se pueden confundir con otra cosa.
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[a-z]{2,}", re.IGNORECASE)
_CEDULA_RE = re.compile(r"\b\d{10}\b")


@dataclass(frozen=True)
class Registro:
    """Nota determinista de una sesion de registro."""
    stars: int
    label: str
    rationale: str
    espera: timedelta | None   # del traspaso de datos a la entrega de credenciales
    entrego: bool
    convirtio: bool            # logro el deposito en la misma sesion


_COACHING = {
    2: "El cliente entrego sus datos y nunca recibio las credenciales: el alta quedo "
       "a medias. Si no podes crearla en el momento, decile cuando la va a tener.",
    3: "Las credenciales tardaron mas de 5 minutos desde que el cliente paso sus "
       "datos. Es el momento de mayor riesgo de que se caiga: crear la cuenta rapido.",
    4: "La cuenta quedo creada. El paso que falta es acompañarlo hasta la primera "
       "recarga, que es donde el registro se convierte en jugador.",
}
_COACHING_1 = ("El cliente entrego sus datos y nadie le respondio. Es el peor momento "
               "para dejarlo colgado: ya habia decidido registrarse.")


def _datos_del_cliente(messages: list[dict]):
    """Primer mensaje del CLIENTE con datos personales de alta. None si no hay."""
    for m in sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"]):
        if m.get("from_me"):
            continue
        body = m.get("body") or ""
        if _EMAIL_RE.search(body) or _CEDULA_RE.search(body):
            return m
    return None


def _credenciales_del_operador(messages: list[dict]):
    """Primer mensaje del OPERADOR que ENTREGA credenciales. None si no hay."""
    for m in sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"]):
        if _is_operator(m) and operator_sent_credentials([m]):
            return m
    return None


def es_transaccion(messages: list[dict]) -> bool:
    """Hubo un alta de verdad, no una consulta sobre como registrarse.

    Alcanza con CUALQUIERA de las dos puntas: que el cliente haya entregado sus datos
    (aunque no le hayan dado nada — ese es justamente el 2) o que el operador haya
    entregado credenciales (el cliente pudo pasar los datos por otro canal: 25 de 707
    casos medidos). Si no hay ninguna de las dos, nadie se registro.
    """
    if not tiene_reloj(messages):
        return False
    return (_datos_del_cliente(messages) is not None
            or _credenciales_del_operador(messages) is not None)


def calificar_registro(messages: list[dict]) -> Registro | None:
    """Nota determinista de la sesion. None si no hubo un alta que calificar."""
    if not es_transaccion(messages):
        return None
    reales = sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    datos = _datos_del_cliente(reales)
    cred = _credenciales_del_operador(reales)
    convirtio = deposit_candidate_count(reales) > 0
    espera = (cred["created_at"] - datos["created_at"]
              if cred and datos and cred["created_at"] > datos["created_at"] else None)

    def _mins(td: timedelta | None) -> str:
        return "nunca" if td is None else f"{td.total_seconds() / 60:.1f} min"

    if cred is None:
        # El alta arranco y no llego. Distinguimos "nadie contesto" de "contesto y no
        # entrego": lo primero es peor, el cliente ya habia decidido registrarse.
        hubo_respuesta = any(
            _is_operator(m) for m in reales
            if datos is not None and m["created_at"] > datos["created_at"])
        if not hubo_respuesta:
            return Registro(1, "mala",
                            "El cliente entrego sus datos y nadie le respondio.",
                            None, False, convirtio)
        return Registro(
            2, "deficiente",
            "El cliente entrego sus datos pero nunca recibio las credenciales: "
            "el alta quedo a medias.",
            None, False, convirtio)
    if convirtio:
        return Registro(
            5, "excelente",
            f"Creo la cuenta ({_mins(espera)} desde el traspaso de datos) y ademas "
            "logro que el cliente depositara en la misma sesion.",
            espera, True, True)
    if espera is None or espera > ENTREGA_AGIL:
        return Registro(
            3, "aceptable",
            f"Entrego las credenciales pero tardo {_mins(espera)} desde que el "
            "cliente paso sus datos (el objetivo son 5 min).",
            espera, True, False)
    return Registro(
        4, "buena",
        f"Creo la cuenta en {_mins(espera)} desde el traspaso de datos; no llego "
        "a la primera recarga.",
        espera, True, False)


def score_registro(messages: list[dict]) -> ScoreResult | None:
    """La nota como ScoreResult, lista para build_score_record. SIN LLM.

    None cuando no hubo alta: una consulta sobre como registrarse se juzga por si el
    cliente entendio la respuesta, no por unas credenciales que nadie pidio.
    """
    r = calificar_registro(messages)
    if r is None:
        return None
    return ScoreResult(
        rubric="registro",
        motivo="registro",
        rating_label=r.label,
        stars=r.stars,
        rating_rationale=r.rationale,
        dimensions={
            "espera_credenciales_seg": (int(r.espera.total_seconds())
                                        if r.espera is not None else None),
            "entrego_credenciales": r.entrego,
            "convirtio_a_deposito": r.convirtio,
        },
        llm_model=MODELO_DETERMINISTA,
        # `registro` es el UNICO motivo donde el eje comercial es el objetivo mismo:
        # el 5 es la conversion. Por eso no hace falta un `atencion` aparte.
        atencion="empujo" if r.convirtio else None,
        deposit_observed=r.convirtio,
        floor_applied=False,
        recomendacion="" if r.stars == 5 else (
            _COACHING_1 if r.stars == 1 else _COACHING[r.stars]),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )
