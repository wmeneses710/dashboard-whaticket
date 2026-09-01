"""CAPA 2: el modelo decide si el operador confirmo la acreditacion cuando el patron no la vio.

EL PROBLEMA QUE CIERRA. `deposito.py` afirma "nunca le confirmo al cliente que la plata habia
entrado" y baja la nota a 2 estrellas, apoyandose en `signals.operator_acreditacion`, que es
un patron. Y UN PATRON PUEDE PROBAR PRESENCIA, NUNCA AUSENCIA: "no matcheo" y "no existe" no
son la misma frase, y `deposito.py` las trataba como si lo fueran. Los cinco huecos de
vocabulario tapados entre el 2026-08-11 y el 2026-09-01 fueron los cinco del mismo lado --
ninguno afirmo una confirmacion de mas, todos afirmaron una ausencia que era falsa. El quinto
concentro 62 notas de 2 estrellas en UNA operadora, porque el hueco era SU plantilla.

POR QUE NO ES "TODO AL MODELO", que es la pregunta obvia. MEDIDO contra gemma4:12b el
2026-09-01 sobre 304 interacciones reales de `deposito`, en dos grupos:

  GRUPO NEG (151) -- las que hoy salen en 2 estrellas por "nunca le confirmo":
      tienen confirmacion REAL .................... 102 de 151 (67,5%)
      el modelo contradice al patron en la
      direccion peligrosa (patron si / modelo no) .   0 de 151   <- el error CARO
      giros que el patron pierde y el modelo ve ...  20, con cita verificable 20/20

  GRUPO POS (153) -- control, las que el patron YA ve:
      el modelo las NEGARIA ....................... 39 de 153 (25,5%)
      de esas, jerga del CRM ("ing"/"ingreso") .... 38 de 39

El patron sabe el VOCABULARIO DEL NEGOCIO -- "ing" no es español, es taquigrafia de este CRM
(`signals.py:26`) y ningun modelo general la va a adivinar. El modelo sabe el IDIOMA: agarra
"Se ecuentra lista su saldo" CON la falta de ortografia, y las formas de confirmar en idioma
humano no se terminan de enumerar nunca. Por eso se pregunta SOLO cuando el patron dice que
no: los "ing" no llegan al modelo y no se pierde la jerga. Preguntar siempre destruiria 1 de
cada 4 confirmaciones buenas -- creando notas injustas nuevas para tapar las viejas.

  El unico desacuerdo del control que NO era jerga resulto ser un falso positivo REAL del
  patron: "debe esperar que culmine el torneo para que se acrediten las ganancias", donde
  `_ACREDITA_FUERTE_RE` lee el subjuntivo `acrediten` como un hecho. 1 en 153 (0,65%) contra
  67,5% del otro lado: la asimetria es de dos ordenes de magnitud, no infinita.

COSTO: 7,6 inferencias por dia sobre las ~350 que ya corren (+2,2%), y acotado por
construccion -- solo el 4,7% de los depositos llega a preguntarse. Si esa poblacion creciera,
eso mismo seria la alarma de que algo cambio.

LA CITA ES EL CINTURON. Se le exige al modelo la frase EXACTA que prueba la confirmacion y se
verifica que exista en el texto del OPERADOR. En los 304 casos acerto 102 de 102, pero la
nota de una persona no se apoya en una racha. Sin cita verificable no hay confirmacion, y una
tilde FALSA es peor que una nota baja: le afirma al negocio algo que nunca paso (el caso que
lo probo esta en `signals._PROMESA_1A_RE`).

CUALQUIER FALLO DEVUELVE None -- sin LLM, timeout, JSON roto, cita inventada. `None` no es
`False`: `False` es una decision del modelo y `None` es un fallo, y el llamador deja la nota
como esta hoy. Una inferencia que no llego nunca cambia una nota.
"""
from __future__ import annotations

import re

from src.signals import _strip_accents, operator_acreditacion

_SISTEMA = (
    "Sos un auditor de atencion al cliente de una plataforma de recargas. "
    "Leés los mensajes del OPERADOR y respondés UNA sola pregunta con precision. "
    "Respondés SOLO JSON.")

_SCHEMA = {
    "type": "object",
    "properties": {"confirmo": {"type": "boolean"}, "frase": {"type": "string"}},
    "required": ["confirmo", "frase"],
}

# Para comparar la cita con el texto real: sin acentos, sin puntuacion, sin emoji y con los
# espacios colapsados. El modelo devuelve la frase con la puntuacion que se le antoja, y lo
# que se verifica es que las PALABRAS esten, no los signos.
_SOLO_PALABRAS_RE = re.compile(r"[^a-z0-9ñ ]+")
_ESPACIOS_RE = re.compile(r"\s+")
# Una cita de dos letras entra en cualquier texto por casualidad. No prueba nada.
_CITA_MINIMA = 4


def _normalizar(texto: str) -> str:
    limpio = _SOLO_PALABRAS_RE.sub(" ", _strip_accents(texto or "").lower())
    return _ESPACIOS_RE.sub(" ", limpio).strip()


def _texto_del_operador(messages: list[dict]) -> str:
    return " ".join(m.get("body") or "" for m in messages
                    if m.get("from_me") and not m.get("is_note"))


def necesita_revisar(messages: list[dict]) -> bool:
    """True si vale gastar una inferencia: el patron no vio nada Y hay algo que leer.

    LOS DOS CORTES SON EL PRESUPUESTO Y LA SEGURIDAD, en ese orden:

    1. Si `operator_acreditacion` YA vio la confirmacion no se pregunta. Es el guard que
       protege la jerga: 38 de los 39 desacuerdos del control son "ing", y preguntar igual
       tiraria 1 de cada 4 confirmaciones buenas.
    2. Si el operador no escribio texto no hay nada que leer. Ahi la ausencia SI es
       verificable sin modelo, y encima esa rama la atiende otra nota ("nadie le respondio").
    """
    if operator_acreditacion(messages):
        return False
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
        "PREGUNTA: ¿el OPERADOR le avisó al cliente que su recarga YA quedó acreditada "
        "(el dinero YA está en la cuenta)?\n\n"
        "Responde true SOLO si el operador afirma un hecho CONSUMADO. Ejemplos de true: "
        "'está listo', 'su recarga ya está acreditada', 'ya puede jugar'.\n"
        "Responde false si el operador solo avisa que está EN CURSO o lo PROMETE. "
        "Ejemplos de false: 'estamos realizando su recarga', 'su comprobante está siendo "
        "verificado', 'en breve verá su saldo', 'ya le cargo'. Tambien false si no dijo nada.\n\n"
        'Formato: {"confirmo": true|false, "frase": "la frase EXACTA del operador que lo '
        'prueba, o cadena vacia si confirmo es false"}')


def confirmo_segun_el_modelo(messages: list[dict], llm) -> bool | None:
    """True = confirmo. False = no confirmo. None = NO SE PUDO decidir (el llamador no cambia).

    NUNCA levanta. Sin LLM, timeout, JSON roto, `confirmo` que no es bool, o una cita que no
    aparece en lo que dijo el OPERADOR -> None.
    """
    if llm is None:
        return None
    try:
        r = llm.chat_json(_SISTEMA, _prompt(messages), schema=_SCHEMA)
    except Exception:  # noqa: BLE001 - una inferencia que falla no puede cambiar la nota
        return None
    if not isinstance(r, dict):
        return None
    confirmo = r.get("confirmo")
    # Un "quiza" no se lee como confirmacion: eso inventaria el merito que este modulo
    # existe para no inventar. Solo un bool de verdad decide.
    if confirmo is False:
        return False
    if confirmo is not True:
        return None
    cita = _normalizar(str(r.get("frase") or ""))
    if len(cita) < _CITA_MINIMA:
        return None
    # LA CITA SE VERIFICA CONTRA EL OPERADOR, no contra la conversacion: que el CLIENTE diga
    # "ya me llego" no es una confirmacion del operador, y es el parafraseo mas facil de creer.
    return True if cita in _normalizar(_texto_del_operador(messages)) else None
