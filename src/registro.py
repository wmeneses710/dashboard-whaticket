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
from src.interacciones import interaccion_de
from src.rubrics import formato_espera
from src.scorer import ScoreResult
from src.signals import (
    _cliente_lo_leyo,
    _is_operator,
    operator_sent_credentials,
    tiene_reloj,
)
# La espera se mide en HORARIO DE ATENCION (ver src/horario.py): 26 por ciento de los
# deficientes eran clientes que escribieron de madrugada y operadores que contestaron
# ni bien abrio el turno. La noche no es una demora del operador.
from src.horario import espera_efectiva
from src.operators import inicio_del_reloj

MODELO_DETERMINISTA = "determinista/registro-v1"

# EL RECHAZO VALIDO: el alta NO PODIA salir y no es culpa del operador -- casi siempre porque
# el cliente YA TIENE cuenta, con otro agente o con la plataforma. Ahi su trabajo es DECIRLO,
# y por eso hay una rama propia (ver calificar_registro). Espejo de `deposito._RECHAZO_RE`.
#
# EL GUARD DE LA NEGACION NO ES COSMETICO: "este numero no esta registrado" es el operador
# PIDIENDO datos, lo OPUESTO a un rechazo, y sin el lookbehind daba rechazo en 12 de los 86
# candidatos medidos el 2026-08-12. Y `ya tienes cuenta` exige el "ya": "no tienes cuenta"
# no matchea porque no lo lleva.
_RECHAZO_RE = re.compile(
    r"ya (tienes|tiene|ten[eé]s|posee|posees) (una |la )?cuenta"
    r"|(la )?cuenta (ya )?(existe|est[aá] (creada|registrada)|esta duplicada)"
    r"|(?<!no )(est[aá]s?|se encuentra) registrad[oa]"
    r"|(?<!no )(est[aá]|se encuentra) (registrada )?(bajo|con) otr[ao]",
    re.IGNORECASE)

ENTREGA_AGIL = timedelta(minutes=5)   # del traspaso de datos a las credenciales

# Datos personales que el cliente manda para que le creen la cuenta. El correo y la
# cedula son los dos campos del formulario que no se pueden confundir con otra cosa.
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[a-z]{2,}", re.IGNORECASE)
_CEDULA_RE = re.compile(r"\b\d{10}\b")
# EL FORMULARIO BANCARIO NO ES UN TRASPASO DE DATOS DE ALTA. En Ecuador una corrida de 10
# digitos es la cedula, pero tambien el celular Y el numero de cuenta que viaja en el pedido
# de retiro. Como el ancla elige el ULTIMO traspaso de la sesion (v12), un pedido de retiro
# POSTERIOR y sin relacion con el alta se volvia el ancla y la ventana juzgada saltaba ahi.
# CASO REAL `bcfc1510` (auditoria del 2026-08-13): Arturo entrego credenciales el 10-ago; dos
# dias despues llego "60 / Vivien Perdomo Poveda / 0802930271 / Banco del Pichincha / Ahorros"
# y la fila salio con 2 estrellas y "el alta quedó a medias" -- falso, y acusando a una
# persona con nombre.
# NO se reusan `_FORMULARIO_RE`/`_MONTO_RE` de src/retiro.py porque ese mensaje real **no
# contiene las palabras "retiro" ni "monto"**: lo que lo delata es el BANCO.
# MEDIDO sobre la copia: de 70.559 mensajes del cliente con una corrida de 10 digitos, 55.890
# (79,2%) traen vocabulario bancario y solo 6.449 traen email.
_FORM_BANCARIO_RE = re.compile(
    r"\b(banco|bco|pichincha|produbanco|guayaquil|pac[ií]fico|pacifico|austro|bolivariano"
    r"|jep|cooperativa|ahorros?|corriente|tipo de cuenta|n[uú]mero de cuenta"
    r"|cuenta de ahorro)\b",
    re.IGNORECASE)


def _es_traspaso_de_datos(body: str) -> bool:
    """El mensaje del cliente trae los datos del ALTA (no un formulario de banco).

    El EMAIL gana siempre: es el unico campo del formulario de alta que no se puede
    confundir con otra cosa. La cedula sola se descarta unicamente cuando el mensaje es
    claramente bancario, para no romper el camino normal -- muchisimos clientes pasan sus
    datos sin email y esa cedula tiene que seguir anclando el alta.
    """
    if _EMAIL_RE.search(body):
        return True
    return bool(_CEDULA_RE.search(body)) and not _FORM_BANCARIO_RE.search(body)


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
    2: "El alta quedó a medias. Si la cuenta no se puede crear en el momento, conviene "
       "decirle cuándo la va a tener: ya entregó sus datos y está esperando.",
    3: "El usuario y la clave tardaron más de 5 minutos desde que el cliente pasó sus "
       "datos. Es el momento de mayor riesgo de que se caiga: conviene crear la cuenta "
       "cuanto antes.",
    # REESCRITO el 2026-08-13. El texto anterior era "La cuenta quedó creada. Lo que falta es
    # acompañarlo hasta la primera recarga, que es donde el registro se convierte en jugador."
    # -- la MISMA clausula que el reproche del rationale de esta rama ("No llegó a acompañarlo
    # hasta la primera recarga"). MEDIDO sobre el rescore v13: 1.040 de 1.054 filas (98,7%) de
    # `registro` determinista en 4 estrellas repetian la frase en los dos campos; en el camino
    # LLM pasaba 0 de 1.541 veces, o sea que era puramente este texto fijo. Decir dos veces
    # QUE falto no es coaching. Ahora dice COMO: los dos pasos concretos, en el momento en que
    # el cliente todavia esta en la conversacion.
    4: "El alta salió rápido y ese es el momento de mayor intención del cliente. Conviene "
       "pasarle los medios de pago ahí mismo y no cerrar la conversación hasta que llegue "
       "el comprobante de la recarga.",
}
# La rama del rechazo tiene su propio consejo: el del 2 dice "decile cuándo la va a tener" y
# ahí NUNCA la va a tener -- la cuenta no se puede crear. Ese texto delante de un operador que
# hizo lo correcto es peor que no decir nada.
_COACHING_RECHAZO_RAPIDO = (
    "Avisaste rápido que la cuenta no se podía crear. Lo que suma es asegurarte de que "
    "llegue a quien sí puede ayudarlo: pasarle el contacto y verificar que lo recibió.")
_COACHING_RECHAZO_TARDE = (
    "El aviso llegó tarde: el cliente estuvo esperando una cuenta que no iba a llegar. "
    "Cuando el alta no puede salir, conviene decirlo apenas se sabe.")
_COACHING_1 = ("El cliente entregó sus datos y nadie le respondió. Conviene acusar el "
               "recibo enseguida: ya había decidido registrarse y es el peor momento "
               "para dejarlo esperando.")


def _datos_del_cliente(messages: list[dict]):
    """El ULTIMO traspaso de datos del cliente: el que elige la interaccion. None si no hay.

    El ANCLA elige la ULTIMA visita: antes tomaba la primera y una sesion mergea todos los
    episodios del ticket (mediana 8,6 h de separacion entre la primera y la ultima, p90 12
    dias, max 266). El 82% de esas sesiones tienen mas de un operador, asi que juzgar la
    primera le cargaba la nota a quien atendio la visita vieja. Ver src/deposito.py para los
    numeros completos. DENTRO de la ventana el reloj arranca en el PRIMER de la visita.
    """
    traspasos = _traspasos_del_cliente(messages)
    return traspasos[-1] if traspasos else None


def _traspasos_del_cliente(messages: list[dict]) -> list[dict]:
    """Mensajes del CLIENTE con datos personales de alta, en orden cronologico.

    EL CONTEXTO ES EL MENSAJE ANTERIOR DEL OPERADOR (2026-08-13). Mirar solo el cuerpo del
    cliente cierra el caso del formulario bancario COMPLETO pero no el mas comun: el operador
    pide los datos del banco y el cliente contesta SOLO el numero. Caso `fda5a4f9`, encontrado
    auditando este mismo arreglo: el operador entrego credenciales el 28-jul; doce dias despues
    pidio "nombre completos de titular, numero de cuenta, banco, cedula" y el cliente escribio
    "2101059380". El ancla saltaba a ese numero, la ventana se recortaba al retiro, y la fila
    salia 2 estrellas con "el alta quedo a medias" -- falso.
    """
    out = []
    ultimo_op = ""
    for m in sorted((m for m in messages if not m.get("is_note")),
                    key=lambda m: m["created_at"]):
        if _is_operator(m):
            ultimo_op = m.get("body") or ""
            continue
        # El operador venia pidiendo datos BANCARIOS: el numero suelto los contesta a ellos,
        # no al alta. El email sigue ganando (lo resuelve `_es_traspaso_de_datos`).
        if (_FORM_BANCARIO_RE.search(ultimo_op)
                and not _EMAIL_RE.search(m.get("body") or "")):
            continue
        if m.get("from_me"):
            continue
        body = m.get("body") or ""
        if _es_traspaso_de_datos(body):
            out.append(m)
    return out


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


def interaccion_juzgada(messages: list[dict]) -> list[dict] | None:
    """La ventana que `calificar_registro` va a juzgar. None si no hubo un alta.

    El ancla es el traspaso de datos del cliente, y si no lo hay (paso los datos por otro
    canal: 25 de 707 casos) la entrega de credenciales del operador — las dos puntas de
    `es_transaccion`. Espejo de deposito/retiro; ver src/interacciones.py.
    """
    if not es_transaccion(messages):
        return None
    ancla = _datos_del_cliente(messages) or _credenciales_del_operador(messages)
    return None if ancla is None else interaccion_de(messages, ancla)


def calificar_registro(messages: list[dict]) -> Registro | None:
    """Nota determinista de la sesion. None si no hubo un alta que calificar."""
    if not es_transaccion(messages):
        return None
    # LA EVIDENCIA SE BUSCA EN LA INTERACCION DEL ALTA. `registro` se habia quedado afuera
    # del ventaneo que `aaadca7` le dio a deposito y retiro, y sobre la sesion entera
    # emparejaba altas distintas: los `datos` de la interaccion 1 con las `cred` de la 5 (una
    # espera inventada), y peor, `convirtio` -- lo que habilita el 5 -- agarraba una recarga
    # de CUALQUIER interaccion mientras el texto afirma "en la misma conversacion".
    # HALLADO el 2026-08-12 auditando v6: `c4a69129` decia "Creo la cuenta 1,3 minutos
    # despues de recibir los datos" con 20.226 minutos (14 dias) de primera respuesta al lado.
    ventana = interaccion_juzgada(messages) or messages
    reales = sorted((m for m in ventana if not m.get("is_note")),
                    key=lambda m: m["created_at"])
    # DENTRO de la ventana el reloj arranca en el PRIMERO de ESA visita: el ancla elige la
    # interaccion (la ultima), el reloj mide la espera COMPLETA. Contar desde el ultimo
    # mensaje del tramo esconderia la demora cuando el cliente insiste dos veces seguidas.
    traspasos = _traspasos_del_cliente(reales)
    datos = traspasos[0] if traspasos else None
    cred = _credenciales_del_operador(reales)
    convirtio = deposit_candidate_count(reales) > 0
    # EL RELOJ ARRANCA CUANDO EL OPERADOR PUEDE RESPONDER (ver src/operators.inicio_del_reloj):
    # entre que el cliente pasa sus datos y el operador entrega las credenciales puede haber
    # una cola que no es suya. v15 aplico esto a deposito/retiro/info y dejo a `registro`
    # afuera sin que ninguna decision lo registrara (a diferencia de `soporte`, cuya exclusion
    # SI esta documentada). MEDIDO el 2026-08-14: de 1.551 filas deterministas, 88 tienen cola
    # sin descontar y 57 de mas de 5 minutos -- justo el umbral de ENTREGA_AGIL.
    # Se busca en `ventana` (que conserva las notas del CRM, a diferencia de `reales`) y desde
    # el traspaso: una entrega anterior significa que el operador YA tenia la conversacion.
    # UNA ENTREGA POSTERIOR A LA ENTREGA NO PROBO NINGUNA COLA. Si la nota del CRM llega
    # DESPUES de que el operador ya dio las credenciales, es que venia trabajando la
    # conversacion antes de que se la formalizaran: no hay cola, y el principio de
    # `inicio_del_reloj` es no inventar la que no se puede probar.
    # MEDIDO el 2026-08-14: sin este guard, 6 de las 8 filas que mejoraban quedaban en cero
    # y el texto salia "Creó la cuenta 0 segundos después de recibir los datos".
    inicio = None
    if datos is not None:
        inicio = inicio_del_reloj(ventana, datos["created_at"])
        if cred is not None and inicio > cred["created_at"]:
            inicio = datos["created_at"]
    espera = (espera_efectiva(inicio, cred["created_at"])
              if cred and datos and cred["created_at"] > datos["created_at"] else None)

    def _mins(td: timedelta | None) -> str:
        return formato_espera(None if td is None else td.total_seconds())

    if cred is None:
        # El alta arranco y no llego. Distinguimos "nadie contesto" de "contesto y no
        # entrego": lo primero es peor, el cliente ya habia decidido registrarse.
        hubo_respuesta = any(
            _is_operator(m) for m in reales
            if datos is not None and m["created_at"] > datos["created_at"])
        if not hubo_respuesta:
            return Registro(1, "mala",
                            "El cliente entregó sus datos y nadie le respondió.",
                            None, False, convirtio)
        # LA RAMA DEL RECHAZO. Si el alta no podia salir por una razon valida -- el cliente
        # ya tiene cuenta -- el trabajo del operador es AVISARLO, y se califica por la
        # velocidad de ese aviso. TECHO EN 4 sin que haga falta pedirlo: el 5 de `registro`
        # es la conversion a deposito y se evalua mas abajo, fuera de este bloque.
        # Solo aplica cuando NO se entregaron credenciales: "ya tienes cuenta creada, tu
        # usuario es X" es un alta EXITOSA, no un rechazo (96 de los casos medidos).
        rechazo = next((m for m in reales
                        if _is_operator(m) and _RECHAZO_RE.search(m.get("body") or "")
                        and (datos is None or m["created_at"] > datos["created_at"])), None)
        if rechazo is not None:
            # Mismo reloj que la entrega de credenciales: el aviso tampoco puede salir
            # antes de que el CRM le entregue la conversacion al operador.
            aviso = (espera_efectiva(inicio, max(rechazo["created_at"], inicio))
                     if datos is not None and inicio is not None else None)
            if aviso is not None and aviso <= ENTREGA_AGIL:
                return Registro(
                    4, "buena",
                    f"La cuenta no se podía crear (el cliente ya tenía una) y lo avisó "
                    f"{_mins(aviso)} después de recibir los datos.",
                    aviso, False, convirtio)
            return Registro(
                3, "aceptable",
                "La cuenta no se podía crear (el cliente ya tenía una), pero el aviso "
                f"tardó {_mins(aviso)} desde que el cliente pasó sus datos."
                if aviso is not None else
                "La cuenta no se podía crear (el cliente ya tenía una) y lo avisó.",
                aviso, False, convirtio)
        return Registro(
            2, "deficiente",
            "El cliente entregó sus datos pero nunca recibió su usuario y clave: "
            "el alta quedó a medias.",
            None, False, convirtio)
    # `espera` es None cuando las credenciales salieron ANTES de que el cliente pasara los
    # datos: los dio por otro canal (25 de 707 casos, ver `es_transaccion`). Ahi la espera NO
    # SE PUEDE MEDIR, y la frase no puede afirmar una duracion: `formato_espera(None)` es
    # "nunca", correcto en "nunca envio el comprobante" y absurdo como duracion.
    # MEDIDO el 2026-08-12 sobre el respaldo v5: 14 filas decian "Creo la cuenta NUNCA
    # despues de recibir los datos" y LAS 14 tenian 5 estrellas, mas 43 con "tardo nunca".
    # Se corrige el TEXTO, no la nota.
    if convirtio:
        detalle = (f" {_mins(espera)} después de recibir los datos"
                   if espera is not None else "")
        return Registro(
            5, "excelente",
            f"Creó la cuenta{detalle} y además logró que el cliente recargara en la "
            "misma conversación.",
            espera, True, True)
    if espera is None:
        return Registro(
            3, "aceptable",
            "Entregó el usuario y la clave, pero no se puede medir cuánto tardó: las "
            "credenciales salieron antes de que el cliente pasara sus datos.",
            None, True, False)
    if espera > ENTREGA_AGIL:
        return Registro(
            3, "aceptable",
            f"Entregó el usuario y la clave, pero tardó {_mins(espera)} desde que el "
            "cliente pasó sus datos. El objetivo son 5 minutos.",
            espera, True, False)
    return Registro(
        4, "buena",
        f"Creó la cuenta {_mins(espera)} después de recibir los datos. No llegó a "
        "acompañarlo hasta la primera recarga.",
        espera, True, False)


def _coaching(r: Registro) -> str:
    """El consejo de la nota. La rama del rechazo tiene el suyo: un 4 o un 3 SIN credenciales
    entregadas solo puede venir de ahi (en el camino normal el 4 y el 3 siempre entregaron),
    y el consejo generico del 3 habla de crear la cuenta cuanto antes -- que es justo lo que
    NO se podia hacer."""
    if r.stars in (3, 4) and not r.entrego:
        return _COACHING_RECHAZO_RAPIDO if r.stars == 4 else _COACHING_RECHAZO_TARDE
    return _COACHING_1 if r.stars == 1 else _COACHING[r.stars]


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
        # None = NO OBSERVO. `convirtio` sigue decidiendo el 5 (es la conversion), pero NO
        # sirve para reconciliar `deposit_mismatch`: desde el ventaneo por interaccion del
        # 2026-08-12 mira SOLO la interaccion del alta, mientras el gate mira la sesion
        # entera -- ventanas distintas, mismatch sistematico. MEDIDO: 8 de los 48 mismatches
        # de la corrida v6 eran estos. El flag reconcilia el gate contra el LLM, y una
        # rubrica determinista no tiene opinion que reconciliar.
        deposit_observed=None,
        floor_applied=False,
        recomendacion="" if r.stars == 5 else _coaching(r),
        claridad="claro",
        friccion=False,
        aciertos=[],
    )


# --- EL CLIENTE PIDIO REGISTRARSE Y NUNCA LE PIDIERON LOS DATOS -------------------
# MEDIDO el 2026-08-07 sobre 2.549 sesiones donde el CLIENTE pide registrarse explicito:
# el operador NO va al punto en **972 (38,1%)**. Con pedido de datos el alta cierra ~40%;
# sin pedido, **12,8%**. Y de esas 972, en **510 (52,5%) el cliente SIGUIO escribiendo** —
# habia conversacion viva y el pedido nunca llego.
#
# LA HIPOTESIS DE LA VERBOSIDAD SE DESCARTO. El negocio sospechaba que el relleno previo
# aburria al cliente; medido: 0 relleno cierra 41,5%, 1 mensaje 46,3%, 2-3 mensajes 30,7%,
# pero 4+ SUBE a 64,9%. Contar mensajes del operador correlaciona con cliente enganchado
# (el operador esta respondiendo preguntas), asi que la causalidad se invierte. Lo que pesa
# no es cuanto hablo antes de ir al punto: es que nunca fue.
# 'quisiera' va junto a 'quiero': es la forma cortés y en estos datos es igual de comun.
# Sin ella los DOS techos (`nunca_pidio_los_datos` y `le_devolvio_la_pelota`) quedaban
# apagados por una letra. Caso `e7d9f25a`: el cliente escribe "Quisiera reGistrarme", el
# modelo lista "No se guio el proceso de registro ni se creo la cuenta" y su rationale dice
# "El operador no atendio el motivo principal del cliente, que era registrarse" — y la nota
# salio 'buena'. De 6 sesiones con esa forma, 5 (83%) quedaron en 4 estrellas.
_QUIERE = r"(quiero|quisiera|querr[ií]a|me gustar[ií]a)"
_QUIERE_REGISTRARSE_RE = re.compile(
    rf"{_QUIERE} registrarme|me {_QUIERE} registrar|{_QUIERE} crear (una |mi )?cuenta|"
    rf"{_QUIERE} (abrir|tener) (una |mi )?cuenta|como me registro|como puedo registrarme|"
    rf"c[oó]mo (hago para )?(me )?registr|{_QUIERE} una cuenta|necesito una cuenta|"
    r"reg[íi]strame|me registro",
    re.IGNORECASE,
)

# EL PUNTO: pedir los datos u ofrecer crear la cuenta. Es el mecanismo real de este
# negocio — NO existe un link de registro (ver src/prompts.py y src/recommendations.py).
_AL_PUNTO_RE = re.compile(
    r"te creo (un |tu |la )?(usuario|cuenta)|creo tu (usuario|cuenta)|"
    # "te ayudo A registrarte" y "te ayudo CON EL registro" son la MISMA oferta. El patron
    # solo tenia la primera, asi que la PIEZA 6 castigaba con 2 estrellas al operador que se
    # ofrecio con la otra preposicion (caso `ec84aae1`, hallado el mismo dia que se escribio).
    # EL GRUPO DE LA AYUDA YA NO ES OPCIONAL (2026-08-13). Con `(ayudo ...)?` el patron
    # colapsaba a `te registr` a secas y matcheaba **"te registras"**, que es lo que hace el
    # CLIENTE y no lo que ofrece el operador. Caso `9a83a433`: "...TE REGISTRAS, verificas tu
    # cuenta y con tu primera carga accedes a una freebet..." se leia como "fue al punto",
    # mientras el rationale del LLM decia -- con razon -- "no guio al cliente paso a paso ni le
    # pidio los datos". Se conservan las dos formas de ayuda y se agrega la primera persona
    # ("te registro yo"), que SI es una oferta.
    r"te ayudo (a |con (el|tu|mi) )?registr|te registro\b|te abro (la|tu) cuenta|"
    r"quieres que te (ayude|cree|registre)|queres que te (ayude|cree|registre)|"
    r"me ayudas con (estos |los )?datos|pasame (tus |los )?datos|"
    r"(nombre de usuario|correo electr[oó]nico|numero de celular|n[uú]mero de celular)|"
    r"necesito (tus|los) datos|env[ií]ame (tus|los) datos|ayudame con (estos |los )?datos",
    re.IGNORECASE,
)


# EL RATIONALE AFIRMANDO QUE NO SE PIDIERON LOS DATOS. Es el unico reclamo del texto libre
# que una señal DURA del mismo hecho (`fue_al_punto`) puede desmentir sola.
# DELIBERADAMENTE ESTRECHO: NO incluye "no completo el alta" ni "no cerro el registro",
# que en el fall-through suelen ser CIERTOS -- llegar ahi prueba que la cuenta no se creo.
_RECLAMO_SIN_DATOS_RE = re.compile(
    r"no (se )?(le )?(pidi|solicit)\w*\s+(los\s+|sus\s+|la\s+)?(datos|informaci[oó]n)",
    re.IGNORECASE,
)


def rationale_desmiente_el_pedido(rationale: str | None, messages: list[dict]) -> bool:
    """True si el texto afirma que no se pidieron los datos y la señal dura lo desmiente.

    MEDIDO el 2026-08-14 sobre v15: de las 2.311 filas de `registro` por el camino LLM,
    283 traen ese reclamo y **149 (52,7%) lo hacen EN FALSO** -- `fue_al_punto` da True,
    el operador si los pidio (134 en 'buena', 10 'aceptable', 5 'deficiente'). Las otras
    134 lo dicen con razon, y por eso el guard mira la señal en vez de filtrar el texto.

    POR QUE NO SE USA `_CONTRADICE_RE` de src/rubrics.py: ese patron es para la nota de
    evidencia POR DIMENSION, donde un "pero" invalida el acierto. Sobre el rationale
    general matchea el 78,1% de los 'buena' y el 92,3% de los 'deficiente' (medido el
    mismo dia): borraria casi todo el padron, incluidas las 134 afirmaciones ciertas.
    """
    if not rationale or not _RECLAMO_SIN_DATOS_RE.search(rationale):
        return False
    return fue_al_punto(messages)


def fue_al_punto(messages: list[dict]) -> bool:
    """El operador PIDIO LOS DATOS u OFRECIO CREAR LA CUENTA: el mecanismo real del alta.

    Es la mitad util de `nunca_pidio_los_datos` expuesta sola, porque el techo de `registro`
    necesita distinguir al operador que se ofrecio y se quedo esperando -- al que la decision
    del 2026-08-07 protege -- del que solo recito la plantilla de venta.
    """
    return any(_is_operator(m) and _AL_PUNTO_RE.search(m.get("body") or "")
               for m in messages if not m.get("is_note"))


def nunca_pidio_los_datos(messages: list[dict]) -> bool:
    """El cliente pidio registrarse, siguio ahi, y el operador nunca fue al punto.

    Las CUATRO condiciones, todas necesarias (el criterio de justicia lo puso el negocio:
    "si no hay nada no se podria bajarle porque no seria justo"):
      1. el cliente pidio registrarse EXPLICITAMENTE (la intencion no esta en disputa),
      2. el cliente SIGUIO escribiendo despues (habia con quien hablar),
      3. ningun mensaje del operador pide los datos ni ofrece crear la cuenta,
      4. el alta NO se cerro.

    La 4ta no es redundante, es un GUARD medido: 124 de las 972 sesiones sin patron
    cerraron el alta igual, o sea que `_AL_PUNTO_RE` tiene falsos negativos. Sin ella se
    penalizaria un registro exitoso por estar redactado distinto.

    LA 2da SE APOYA EN `ack` (2026-08-11). Existia porque, con el cliente ido, no se podia
    separar "el operador no tuvo chance" de "se fue porque no le pidieron nada", y la duda
    favorecia al operador. El `ack` de WhatsApp rompe el empate: si el cliente LEYO los
    mensajes del operador, el operador tuvo su chance y no la uso. Con el cliente ido Y sin
    lectura, la duda sigue favoreciendolo. Son 117 sesiones mas de `registro`.
    """
    reales = [m for m in messages if not m.get("is_note")]
    idx = next((k for k, m in enumerate(reales)
                if not m.get("from_me")
                and _QUIERE_REGISTRARSE_RE.search(m.get("body") or "")), None)
    if idx is None:
        return False                                    # 1
    posteriores = reales[idx + 1:]
    del_operador = [m for m in posteriores if _is_operator(m)]
    if not del_operador:
        return False                                    # sin operador -> no_agent_reply
    # 2: habia con quien hablar — el cliente siguio escribiendo, o LEYO lo que le mandaron.
    if not any(not m.get("from_me") for m in posteriores) \
            and not any(_cliente_lo_leyo_de_verdad(m) for m in del_operador):
        return False
    if any(_is_operator(m) and _AL_PUNTO_RE.search(m.get("body") or "")
           for m in reales[idx:]):
        return False                                    # 3: si fue al punto
    if operator_sent_credentials(reales) or es_transaccion(reales):
        return False                                    # 4: el alta se cerro igual
    return True


def _cliente_lo_leyo_de_verdad(m: dict) -> bool:
    """Como `_cliente_lo_leyo`, pero un `ack` AUSENTE cuenta como NO leido.

    La diferencia con la señal de abandono es deliberada y va en la direccion segura de
    cada una. Alla el default es True para no PERDER un abandono cuando la columna no
    viene; aca el default es False para no INVENTAR una penalizacion: sin evidencia de
    lectura, el criterio viejo (la duda favorece al operador) queda intacto.
    """
    return m.get("ack") is not None and _cliente_lo_leyo(m)


# --- DEVOLVERLE LA PELOTA AL CLIENTE QUE YA DECIDIO -------------------------------
# Criterio del negocio (2026-08-11), a partir de un chat concreto: "si el cliente ya dice
# que quiere registrarse, y el operador le pregunta, es una deficiencia". El piso de la
# rubrica de `registro` es "guia el alta de la cuenta paso a paso" (src/rubrics.py):
# repreguntarle la intencion que YA declaro no es guiar, es un paso atras.
#
# LA LINEA FINA: `_AL_PUNTO_RE` ya trata "¿quieres que te CREE la cuenta?" como ir al punto,
# porque el operador se ofrece a ACTUAR — ese es el mecanismo de este negocio. Lo que se
# penaliza es lo contrario: "¿te animas a registrarte?", donde la pelota vuelve al cliente
# que ya la habia pateado. El lookahead `(?!que te)` es exactamente esa frontera.
#
# MEDIDO sobre la copia: 188 sesiones, nota media 3,43 y **82 de ellas con 4 o 5 estrellas**.
_PELOTA_AL_CLIENTE_RE = re.compile(
    r"te anim[aá]s?|"
    r"(quieres|queres|deseas|te gustar[ií]a|te interesa)\s+(?!que\s+te\b)"
    r"(registrarte|registrar|crear (una |tu )?cuenta|abrir (una |tu )?cuenta)",
    re.IGNORECASE,
)


def le_devolvio_la_pelota(messages: list[dict]) -> bool:
    """El cliente pidio registrarse y el operador le repregunto la intencion, sin actuar.

    Las CUATRO condiciones, espejo de `nunca_pidio_los_datos` (y con los mismos guards):
      1. el cliente pidio registrarse EXPLICITAMENTE — la intencion no esta en disputa,
      2. algun mensaje POSTERIOR del operador le devuelve la decision al cliente,
      3. el operador nunca fue al punto (ni pidio datos ni se ofrecio a crear la cuenta),
      4. el alta NO se cerro.

    A DIFERENCIA de `nunca_pidio_los_datos`, esto NO mira si el cliente estaba presente ni
    si leyo: no juzga el resultado sino lo que el operador ESCRIBIO, que es observable haya
    llegado o no. Por eso el caso de Gloria Villacis (ack=2, nunca lo leyo) cae igual.
    """
    reales = [m for m in messages if not m.get("is_note")]
    idx = next((k for k, m in enumerate(reales)
                if not m.get("from_me")
                and _QUIERE_REGISTRARSE_RE.search(m.get("body") or "")), None)
    if idx is None:
        return False                                    # 1
    if not any(_is_operator(m) and _PELOTA_AL_CLIENTE_RE.search(m.get("body") or "")
               for m in reales[idx + 1:]):
        return False                                    # 2
    if any(_is_operator(m) and _AL_PUNTO_RE.search(m.get("body") or "")
           for m in reales[idx:]):
        return False                                    # 3
    if operator_sent_credentials(reales) or es_transaccion(reales):
        return False                                    # 4
    return True


def se_creo_la_cuenta(messages: list[dict]) -> bool:
    """En esta sesion SE CREO una cuenta nueva: el cliente paso sus datos Y el operador
    devolvio usuario y clave.

    Es la señal que fuerza el motivo a `registro` (ver src/scorer.py). Decision del negocio
    del 2026-08-07: "si en una se crea la cuenta es registro independientemente de lo que
    haya antes o despues". La promo o la recarga son el gancho y el cierre; el alta es el
    hecho consumado, y es lo que define con que vara se mide.

    Exige LAS DOS PUNTAS a proposito. Credenciales SIN datos del cliente es un RESETEO de
    contraseña de una cuenta que ya existia (30 sesiones medidas, 18 de ellas del segmento
    agente): ahi `soporte_cuenta` es el motivo correcto y el guard NO debe pisarlo.

    NO usa `_datos_del_cliente` ni `_credenciales_del_operador` a proposito: esos dos
    ordenan por `created_at` porque necesitan el RELOJ (la espera entre las dos puntas), y
    este guard corre en TODAS las sesiones del pase con LLM — incluido el path
    por-conversacion de scripts/, que no trae timestamps. Aca solo importa si las dos cosas
    PASARON, no cuando: es una pregunta de texto, no de tiempo.
    """
    reales = [m for m in messages if not m.get("is_note")]
    dio_datos = any(
        not m.get("from_me") and _es_traspaso_de_datos(m.get("body") or "")
        for m in reales
    )
    entrego_creds = any(_is_operator(m) and operator_sent_credentials([m]) for m in reales)
    return dio_datos and entrego_creds
