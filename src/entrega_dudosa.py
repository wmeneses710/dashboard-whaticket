"""CAPA 2: el modelo decide si el retiro NO se podia pagar, cuando el patron no lo sabe.

EL PROBLEMA QUE CIERRA. `retiro.py` baja a 2 estrellas con "nunca envió el comprobante del
retiro: el agente no tiene con qué respaldar que la plata salió" cuando `entrega is None`.
Pero esa nota da por sentado que HABIA una entrega que hacer, y eso no siempre es cierto:

    "El pago queda pendiente para mañana 🍀a partir de las 6am"   -> la plata no salio aun
    "No cuenta con el saldo a retirar"                            -> no hubo retiro
    "no corresponde el titular con el numero de cuenta"           -> los datos estaban mal

En los tres el operador hizo lo correcto -- avisar por que-- y la rubrica le exige la
prueba de algo que NO OCURRIO. Es la misma regla que ya rige la acreditacion: un patron
puede probar una presencia, nunca una ausencia, y menos aun la ausencia de una OBLIGACION.

POR QUE NO SE ESPEJA EL `_RECHAZO_RE` DE `deposito.py`, que era lo obvio: enumerar frases
es volver al tren del que nos bajamos. "No cuenta con el saldo", "queda pendiente para
mañana", "no corresponde el titular" -- y el mes que viene la sexta forma de decirlo. La
pregunta se la lleva el modelo, que entiende el idioma; el patron se queda con lo que puede
probar.

MEDIDO CONTRA gemma4:12b el 2026-09-01, 119 interacciones reales en tres grupos:

  A EVALUAR (19) -- retiros con la nota "nunca envió el comprobante":
      el modelo absuelve ................  3 (16%), con cita verificable 3 de 3
      las tres, leidas a mano, correctas:  saldo insuficiente, pago diferido, y un
                                           "enviar nuevamente a partir de las 6 am"
      las 16 que NO absuelve dicen "está listo" o "el retiro ya se realizó con éxito"
      SIN mandar la prueba: bien castigadas, porque el comprobante ES el respaldo.

  CONTROL (100) -- 65 recargas acreditadas y 35 retiros efectivamente pagados:
      absoluciones falsas ALCANZABLES ...  0

  El unico "falso positivo" del control resulto ser el modelo teniendo razon: el operador
  SI habia avisado "no corresponde el titular con el numero de cuenta". Pero esa fila cayo
  en la rama "Envió el comprobante, pero tarde" -- la plata salio igual-- y absolverla
  seria perdonar una demora real. POR ESO EL GATE ES `entrega is None` Y NO EL TEXTO: donde
  hay comprobante no se pregunta nada, y ese caso no llega nunca.

LA CITA ES EL CINTURON, igual que en `acreditacion_dudosa`: se exige la frase EXACTA y se
verifica contra el texto del OPERADOR. Sin cita comprobable no hay absolucion. Un perdon
falso es peor que una nota baja: le afirma al negocio que no habia nada que entregar.

CUALQUIER FALLO DEVUELVE None -- sin LLM, timeout, JSON roto, cita inventada -- y la nota
queda como hoy. La capa 2 SOLO PUEDE ABSOLVER.
"""
from __future__ import annotations

from src.acreditacion_dudosa import _normalizar, _texto_del_operador, _CITA_MINIMA

_SISTEMA = (
    "Sos un auditor de atencion al cliente de una plataforma de recargas y retiros. "
    "Leés los mensajes del OPERADOR y respondés UNA pregunta. Respondés SOLO JSON.")

_SCHEMA = {
    "type": "object",
    "properties": {"no_se_podia": {"type": "boolean"}, "frase": {"type": "string"}},
    "required": ["no_se_podia", "frase"],
}


def necesita_revisar(messages: list[dict]) -> bool:
    """True si hay texto del operador para leer. El gate de `entrega is None` lo pone el
    llamador (`retiro.calificar_retiro`), que es el unico que sabe si hubo comprobante."""
    return bool(_normalizar(_texto_del_operador(messages)))


def _prompt(messages: list[dict]) -> str:
    lineas = []
    for m in messages:
        if m.get("is_note"):
            continue
        quien = "OPERADOR" if m.get("from_me") else "CLIENTE"
        media = m.get("media_type") or "chat"
        cuerpo = (m.get("body") or "").strip() or f"[{media}]"
        if media in ("image", "document", "video"):
            cuerpo = f"[adjunta {media}] {cuerpo}".strip()
        lineas.append(f"{quien}: {cuerpo}")
    return (
        "Conversacion:\n---\n" + "\n".join(lineas) + "\n---\n\n"
        "PREGUNTA: ¿el OPERADOR le avisó al cliente que la operación NO se podía "
        "completar?\n\n"
        "Responde true SOLO si el operador dice que NO se hace o que queda para después. "
        "Ejemplos de true: 'no cuenta con el saldo a retirar', 'el pago queda pendiente "
        "para mañana', 'la boleta está repetida', 'el titular no coincide', 'fue "
        "rechazada'.\n"
        "Responde false si la operación SÍ se hizo o está en curso normal. Ejemplos de "
        "false: 'está listo', 'ingresó', 'tu retiro está en proceso', 'en breve te "
        "enviamos el comprobante', o si el operador no dijo nada al respecto.\n\n"
        'Formato: {"no_se_podia": true|false, "frase": "la frase EXACTA del operador que '
        'lo prueba, o cadena vacia si es false"}')


def no_se_podia_segun_el_modelo(messages: list[dict], llm) -> bool | None:
    """True = el operador avisó que no se podía. False = sí se podía. None = NO SE PUDO.

    NUNCA levanta. Sin LLM, timeout, JSON roto, `no_se_podia` que no es bool, o una cita
    que no aparece en lo que dijo el OPERADOR -> None, y el llamador no cambia la nota.
    """
    if llm is None:
        return None
    try:
        r = llm.chat_json(_SISTEMA, _prompt(messages), schema=_SCHEMA)
    except Exception:  # noqa: BLE001 - una inferencia que falla no puede cambiar la nota
        return None
    if not isinstance(r, dict):
        return None
    dice = r.get("no_se_podia")
    if dice is False:
        return False
    if dice is not True:
        return None
    cita = _normalizar(str(r.get("frase") or ""))
    if len(cita) < _CITA_MINIMA:
        return None
    return True if cita in _normalizar(_texto_del_operador(messages)) else None
