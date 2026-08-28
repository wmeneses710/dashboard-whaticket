"""CAPA 2: el modelo decide si un fragmento sin respuesta del negocio es una cola de cortesia.

EL PROBLEMA QUE CIERRA. Cuando el CRM reabre despues de un cierre, el mensaje que el cliente
manda queda como una atencion propia. Si nadie le contesta, `sin_respuesta` le pone
**1 estrella por "nadie le respondio"** -- y muchas veces eso es falso, porque el cliente solo
agradecio. Caso de produccion (2026-08-28, interaccion `319e88cc`): Ramirez acredito bien, el
cliente escribio "Mut amable" **once segundos** despues del cierre, el CRM reasigno a Mario y
**Mario cobro el 1 estrella sin escribir una palabra**.

POR QUE NO ALCANZA CON UN PATRON. `_solo_cortesia_del_cliente` ya usa
`signals.client_sin_motivo`, que es determinista y gratis, pero un patron no absorbe typos.
MEDIDO sobre 30 dias: de los 85 fragmentos que cobran 1 estrella por esto, **61 los salva la
capa determinista y quedan 24** -- menos de UNA inferencia por dia. Por eso el modelo puede
estar SIEMPRE activo: casi nunca se lo llama.

LA POLARIDAD DEL RIESGO MANDA, y la dicto el negocio: *"no es lo mismo no responder a un
gracias, a un ok o a un listo, que no responder a una pregunta"*. Dejar pasar un reclamo
esconde una falla real; castigar un agradecimiento cuesta una estrella. Se implementa en tres
lugares:
  1. el prompt dice explicitamente que ante la duda se PUNTUA;
  2. `necesita_el_modelo` solo deja pasar fragmentos que YA parecen cola (sin mensajes del
     negocio, sin media), asi que un reclamo con comprobante nunca llega a preguntarse;
  3. CUALQUIER fallo devuelve **None**, que el llamador trata como "puntua" -- el
     comportamiento de hoy. Nunca se pierde un reclamo por una inferencia que no llego.

MEDIDO CONTRA gemma4:12b sobre 77 casos reales, muestra MIXTA y balanceada (33 IGNORAR /
44 PUNTUAR, asi que "ignorar a todo" saca 42,9% y no 100%):

    acierto global ................. 76/77 (98,7%)
    planteos que dejaria pasar ......  0 de 44   <- el error CARO
    gracias que castigaria ..........  1 de 33   <- el barato, y fue un '💸' suelto
                                                    que la capa 1 ya resuelve antes

EL MODELO NO ENTRA EN EL CORTE, y eso no es negociable. `partir_en_interacciones` define la
PK (`interaccion_id` = uuid5 de session_id + instante de inicio): un corte no determinista da
ids distintos en cada rescore y deja filas huerfanas. Y `queries.py` lo llama SINCRONICO por
request del tablero, asi que una inferencia ahi es una por carga de pantalla. Esto corre
DESPUES del corte, sobre un fragmento ya cerrado, y es una decision de NOTA.
"""
from __future__ import annotations

# Para la bitacora de errores compartida con el ETL (ver src/errores.py). Su vocabulario de
# componentes esta acordado; este modulo es parte del pase del modelo.
COMPONENTE = "llm"

_SISTEMA = (
    "Eres auditor de atencion al cliente de una casa de apuestas ecuatoriana.\n"
    "El CRM cerro una atencion y el cliente escribio DESPUES del cierre. Nadie del negocio "
    "le respondio.\n"
    "Tu unica tarea: decidir si ese mensaje del cliente EXIGIA una respuesta.\n\n"
    "IGNORAR = el mensaje es la cola de la atencion anterior: agradece, confirma que quedo "
    "conforme, se despide o reacciona con un emoji. No hay nada que contestar ni procesar. "
    "Incluye mensajes mal escritos o con typos ('Graciad', 'Liato', 'Mut amable') y "
    "abreviaturas en otro idioma ('Tks').\n"
    "PUNTUAR = el mensaje plantea algo NUEVO: una pregunta, un reclamo, un pedido, datos "
    "para procesar (montos, cuentas, cedulas), o dice que el problema sigue. Ahi el silencio "
    "del operador es una falla real.\n\n"
    "Si dudas, elegi PUNTUAR: dejar pasar un reclamo sin responder es mucho peor que "
    "calificar de mas un agradecimiento."
)

# TRAMPA 1 de scripts/bench_sin_motivo.py: la opcion de IGNORAR tiene que existir en el enum
# como respuesta de PRIMERA CLASE. Si se pregunta "que motivo tiene?" el modelo elige uno
# siempre, y eso mide el prompt, no al modelo.
_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["IGNORAR", "PUNTUAR"]},
        "cita": {"type": "string"},
    },
    "required": ["decision", "cita"],
}


def _reales(frag: list[dict]) -> list[dict]:
    return [m for m in frag if not m.get("is_note")]


def necesita_el_modelo(frag: list[dict]) -> bool:
    """El fragmento cae en el RESIDUO: parece una cola pero la capa 1 no la reconoce.

    Es el gate que mantiene el costo en 0,8 inferencias por dia, y ademas es la segunda
    barrera de la polaridad del riesgo: lo que no parece cola NI SE PREGUNTA.
    """
    from src.signals import client_sin_motivo

    reales = _reales(frag)
    if not reales:
        return False
    for m in reales:
        # Un mensaje del negocio significa que la atencion existio: no hay nada que ignorar.
        if m.get("from_me"):
            return False
        # Un media suelto es un planteo (un comprobante despues del cierre), no una cola.
        if (m.get("media_type") or "chat") != "chat":
            return False
    # LO QUE ESTA LINEA EXCLUYE, Y LO QUE NO. Si `client_sin_motivo` dice que es cortesia, NO
    # se gasta inferencia: ya esta decidido de forma determinista y el modelo no agrega nada
    # (se lo midio: 40/40 de acuerdo, ver scripts/bench_sin_motivo.py).
    #
    # OJO CON LEER ESTO COMO "esos fragmentos ya no llegan aca". NO ES ASI, y lo verifique: la
    # capa 1 solo los PEGA dentro de `GRACIA_CORTESIA_SEG` (10 min). Pasada la ventana el
    # fragmento existe igual, `client_sin_motivo` sigue diciendo cortesia, y hoy se lleva el
    # 1 estrella lo mismo. MEDIDO sobre 30 dias, de los 65 que cobran 1 estrella:
    #     3  cortesia dentro de la ventana
    #    35  cortesia FUERA de la ventana  <- el bucket mas grande, y nadie lo salva
    #    25  residuo que llega al modelo
    #     2  no parece cola (el 1 estrella esta bien puesto)
    # Que hacer con esos 35 es una DECISION DEL NEGOCIO pendiente -- es la misma que la del
    # `'Ok'` a los 83 minutos -- y no se resuelve con el modelo, porque ahi el determinista ya
    # sabe la respuesta: lo que falta decidir es si un "gracias" tardio merece skip o nota.
    return not client_sin_motivo(reales)


def _prompt(frag: list[dict], ultimo_del_negocio: str | None) -> str:
    textos = [(m.get("body") or "").strip() for m in _reales(frag)]
    cliente = " | ".join(t for t in textos if t)
    return (
        f"ULTIMO MENSAJE DEL NEGOCIO ANTES DEL CIERRE:\n"
        f"{(ultimo_del_negocio or '').strip() or '(ninguno)'}\n\n"
        f"MENSAJE(S) DEL CLIENTE DESPUES DEL CIERRE:\n{cliente}\n\n"
        'Responde JSON: {"decision": "IGNORAR" o "PUNTUAR", "cita": "las palabras del '
        'cliente que lo deciden"}'
    )


def decidir_con_el_modelo(frag: list[dict], ultimo_del_negocio: str | None,
                          llm) -> bool | None:
    """True = es cola de cortesia (ignorar). False = plantea algo (puntuar). None = NO SE PUDO.

    `None` y `False` NO son lo mismo aunque hoy los dos terminen puntuando: `False` es una
    decision del modelo y `None` es un fallo, y el segundo hay que poder contarlo. El llamador
    trata `None` como "puntua", que es el comportamiento de siempre.

    NUNCA levanta: sin LLM, timeout, JSON roto o respuesta fuera del enum -> None.
    """
    if llm is None:
        return None
    try:
        r = llm.chat_json(_SISTEMA, _prompt(frag, ultimo_del_negocio), schema=_SCHEMA)
        decision = (r or {}).get("decision")
        decision = decision.upper() if isinstance(decision, str) else None
    except Exception:  # noqa: BLE001 - una inferencia que falla no puede cambiar la nota
        return None
    if decision == "IGNORAR":
        return True
    if decision == "PUNTUAR":
        return False
    # Cualquier otra cosa es lo mismo que no haber contestado. Un "QUIZA" no se lee como
    # IGNORAR: eso invertiria la polaridad del riesgo justo donde importa.
    return None
