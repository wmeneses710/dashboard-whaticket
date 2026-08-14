"""Orquesta el scoring SEMANTICO de UNA conversacion.

Arma el prompt (con contexto del hilo), llama al LLM para obtener la
calificacion cualitativa, valida las claves y aplica la estrella determinista
desde la etiqueta. El LLM nunca decide la estrella. La elegibilidad (rubrica,
evaluated/skipped) la decide antes el router; aca ya llega una conversacion
'evaluated'.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from src.metrics import hay_persona_del_negocio
from src.prompts import build_motivo_prompt, build_motivo_schema
from src.recommendations import refine_recomendacion
from src.rubrics import MOTIVOS, derive_aciertos, label_from_facts, label_to_stars
from src.signals import (
    cliente_abandono_tras_pedido,
    cliente_confirmo_resuelto,
    operator_acreditacion,
    operator_confirmation,
    operator_maltrato,
    operator_pushed,
    operator_resolved,
    operator_sent_media,
    client_asked_question,
    client_reasked,
)

# Valores validos del eje de CLARIDAD (hecho del LLM). 'dudoso' es el neutral/borderline:
# no demota ni bloquea el uplift. Ausente o invalido -> 'dudoso' (no castigar por omision).
CLARIDAD_VALS = ("claro", "confuso", "dudoso")

# PISO determinista por motivo (¿el operador atendió?, aunque el LLM diga que no):
# - _RESOLVED_FLOOR: transaccional/trámite -> basta una CONFIRMACION o MEDIA del operador
#   (comprobante de retiro, video KYC). Inequívoco.
# - _FUNNEL_FLOOR: front-of-funnel (flujo de anuncio "¿cómo reclamo mis giros?") -> el piso
#   es explicar la promo / mandar el link o formulario / acreditar, NO una respuesta literal
#   a la pregunta del anuncio. Basta RESOLVED o un EMPUJE concreto (pushed).
# 'problema' NO se floorea determinista: resolver un reclamo es difuso y un empuje comercial
# no es resolución -> se deja al modelo.
_RESOLVED_FLOOR = frozenset({"deposito", "retiro", "soporte_cuenta"})
_FUNNEL_FLOOR = frozenset({"info", "promo", "registro"})


class LLM(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict | None = ...) -> dict: ...


@dataclass(frozen=True)
class ScoreResult:
    rubric: str
    dimensions: dict
    rating_label: str
    rating_rationale: str
    stars: int
    llm_model: str
    atencion: str | None            # empujo|pasivo|no_respondio; None si el LLM no dio uno valido
    deposit_observed: bool | None   # observacion LLM del deposito (el gate determinista manda)
    motivo: str | None = None       # pase v2: motivo clasificado por el LLM (None en el pase viejo)
    floor_applied: bool = False     # True si un override determinista cambio un HECHO (ver score_by_motivo)
    recomendacion: str = ""         # consejo accionable para el operador (coaching); "" si excelente
    claridad: str = "dudoso"        # eje claridad EFECTIVO (claro|confuso|dudoso) que modulo la nota
    friccion: bool = False          # True si el cliente tuvo que reinsistir sin respuesta (determinista)
    aciertos: list = field(default_factory=list)  # el "por que" POSITIVO (espejo de errores[])


def _validate(raw: dict, schema: dict) -> None:
    """Verifica que la salida del LLM tenga las claves del RATING (lo unico duro).

    Reemplaza la garantia que daria el schema-grammar (que no usamos): pedimos la
    forma en el prompt y la validamos aca. `atencion`/`deposit_observed` NO son
    duros: si el LLM los omite o los manda mal, NO descartamos un rating por lo
    demas valido (se degradan a None en score_conversation). Un rating sin esos
    ejes es preferible a dejar la conversacion atascada reintentando para siempre.
    """
    for key in schema["required"]:
        if key not in raw:
            raise ValueError(f"salida del LLM sin la clave requerida: {key!r}")
    dims = raw.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("salida del LLM: 'dimensions' debe ser un objeto")
    for key in schema["properties"]["dimensions"]["required"]:
        if key not in dims:
            raise ValueError(f"salida del LLM: falta la dimension {key!r}")


def _as_bool(v):
    """Parseo tolerante de deposit_observed: el fast path (format=json) NO garantiza
    un bool real. bool('false') seria True -> hay que parsear el string.
    None (no vino) o valor AMBIGUO -> None: no inventamos un False que dispararia un
    deposit_mismatch falso; degradamos igual que atencion fuera del enum."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("true", "1", "si", "sí", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None  # ambiguo ("no sé", "", etc.) -> sin observacion


def score_by_motivo(
    *,
    target_messages: list[dict],
    thread_context: str,
    llm: LLM,
    deposit_hint: bool = False,
    recommender=None,
    cierre_at=None,
) -> ScoreResult:
    """Pase v2: el LLM clasifica el MOTIVO (de la tabla) y califica en 2 capas.

    La estrella sigue siendo determinista (label_to_stars). El motivo elegido define
    la rubrica del rating_label (la escala es unica, asi que cualquier motivo valida
    igual). `deposit_hint` inyecta la senal determinista de comprobante en el prompt.
    """
    # El abandono del cliente se calcula ANTES del prompt: es un hecho verificable que el
    # modelo necesita para no reprochar lo que dependia de una respuesta que nunca llego.
    abandono = cliente_abandono_tras_pedido(target_messages)
    system, user = build_motivo_prompt(target_messages, thread_context,
                                       deposit_hint=deposit_hint, abandono_hint=abandono)
    schema = build_motivo_schema()
    raw = llm.chat_json(system, user, schema)
    _validate(raw, schema)

    motivo = raw.get("motivo")
    if motivo not in MOTIVOS:
        raise ValueError(f"motivo invalido del LLM: {motivo!r} (validos: {list(MOTIVOS)})")
    # Guard determinista deposito<->retiro: el deposit_hint viene de un comprobante del
    # CLIENTE (gate en deposits.py), y eso es una RECARGA. En un retiro el comprobante lo
    # manda el OPERADOR. Si el LLM confundio y dijo 'retiro' con hint, se corrige a 'deposito'
    # (arregla el confusor mas comun del modelo y evita "Retiro + Recargado" en el dashboard).
    if deposit_hint and motivo == "retiro":
        motivo = "deposito"
    # problema->deposito: un comprobante del cliente ("Abono a deuda") que el OPERADOR
    # confirmo ("ing"/"acreditado") es una recarga completada, NO un reclamo. Se exige
    # la confirmacion (a diferencia de retiro) para NO pisar un reclamo genuino de
    # deposito no acreditado, donde el operador no confirmo nada.
    #
    # NO SE AMPLIA A promo/info/soporte_cuenta, y esto se PROBO Y SE REVIRTIO el 2026-08-14.
    # La auditoria pedia extenderlo (34 filas de `promo` y 18 de `info` con
    # `deposit_candidate_count>0`, juzgadas con una vara mas laxa que la de deposito). Se
    # implemento y al leer los transcripts salieron FALSOS POSITIVOS en 2 de 2 casos
    # muestreados entre los saltos mas dramaticos (5->2 estrellas):
    #   `e8c60130`: "¿Cómo se recibe el regalo de $5?" -> el cliente crea la cuenta y dice
    #               "De aquí a mañana recargo". NUNCA recargo.
    #   `d14eecd5`: consulta pura sobre condiciones de bono; cierra con "los depósitos los
    #               hago aquí contigo", en futuro. NUNCA recargo.
    # LA CAUSA: `deposit_candidate_count`/`es_transaccion` se disparan de mas cuando la
    # recarga es el TEMA de la charla y no el hecho -- que es exactamente lo que es una
    # sesion de `promo`. Subir la vara a `operator_acreditacion` bajaba de 67 a 51 filas
    # pero NO elimino los falsos positivos.
    # EL DAÑO DE EQUIVOCARSE ES PEOR QUE EL DEL STATUS QUO: la rubrica de deposito le hace
    # decir al tablero "nunca le confirmo al cliente que la plata habia entrado" sobre plata
    # que nunca se movio. `problema` no sufre esto porque ahi la sesion YA es un reclamo
    # sobre una transaccion concreta.
    elif deposit_hint and motivo == "problema" and operator_confirmation(target_messages):
        motivo = "deposito"

    # ALTA CERRADA -> el motivo es `registro`, pise lo que dijo el LLM. Decision del negocio
    # del 2026-08-07. MEDIDO: de 163 altas consumadas (cliente paso datos + operador devolvio
    # usuario y clave), solo 103 quedaban como `registro`; **40 caian en `promo`**, 12 en
    # soporte_cuenta y 2 en deposito. Esas 40 se calificaban con la rubrica de promo — que
    # mide flyer y empuje — cuando el hecho de la sesion fue una CUENTA CREADA, y ademas
    # quedaban fuera de todo el trabajo hecho sobre `registro`.
    # Mismo criterio que ya regia para el deposito dentro de registro: el alta es el hecho
    # consumado y la promo fue el gancho.
    from src.registro import se_creo_la_cuenta

    if se_creo_la_cuenta(target_messages):
        motivo = "registro"

    # DEPOSITO TRANSACCIONAL: la nota la manda la rubrica determinista (src/deposito.py).
    # El LLM ya hizo su trabajo irremplazable — decir que motivo es — y a partir de ahi
    # los tres hechos que definen la nota son verificables: el reloj del comprobante, si
    # confirmo la acreditacion, y si chequeo que al cliente no le faltara nada. Medido:
    # con la escala generica el 86,4% de los depositos caia en 3 estrellas y 135 de 149
    # transacciones perfectas quedaban ahi; al sacar el cap, el 47,5% llegaba a 5 SOLO
    # por cortesia. Ninguna de las dos medía el trabajo.
    # Lo mismo para `retiro`, con la asimetria del motivo: ahi el comprobante lo manda
    # el OPERADOR y es la entrega misma. Ambos separan TRANSACCION de CONSULTA y ceden
    # el turno al pase con LLM cuando el cliente solo pregunto (56,8% en retiro, 52,1%
    # en deposito): sin plata pedida no hay nada que entregar ni que acreditar.
    # Import diferido: los dos modulos importan ScoreResult de este.
    determinista = None
    if motivo == "deposito":
        from src.deposito import score_deposito

        determinista = score_deposito(target_messages, cierre_at)
    elif motivo == "retiro":
        from src.retiro import score_retiro

        determinista = score_retiro(target_messages, cierre_at)
    elif motivo == "registro":
        from src.registro import score_registro

        determinista = score_registro(target_messages)
    elif motivo == "promo":
        from src.promo import score_promo

        determinista = score_promo(target_messages)
    elif motivo == "soporte_cuenta":
        from src.soporte import score_soporte

        determinista = score_soporte(target_messages, cierre_at)
    elif motivo == "info":
        from src.info import score_info

        determinista = score_info(target_messages, cierre_at)

    if determinista is not None:
        # LOS FRAGMENTOS DE NEGOCIO TAMBIEN EN EL CAMINO DETERMINISTA. `refine_recomendacion`
        # se llamaba SOLO abajo (el camino LLM), y las rubricas deterministas retornaban antes
        # sin pasar por ahi. MEDIDO el 2026-08-12: de las 294 filas de
        # `determinista/registro-v1`, **278 entregaron credenciales y NI UNA le decia al
        # cliente que cambie la contraseña** -- una regla de SEGURIDAD que existe en el codigo
        # y no disparaba justo donde mas aplica, porque `registro` es determinista cuando SI
        # hubo alta. En el camino LLM eran 88 de 397 (22%).
        # `agilidad` queda afuera a proposito: no pasa por aca (worker.py la llama directo) y
        # sus fragmentos serian de otro dominio -- un agente no se convierte, opera una caja.
        return replace(determinista, recomendacion=refine_recomendacion(
            determinista.recomendacion, motivo=motivo, target_messages=target_messages))

    # HECHOS del LLM -> etiqueta por CODIGO. El modelo juzga hechos concretos (que hace
    # bien); la regla de 2 capas la aplica label_from_facts (que el modelo aplicaba de
    # forma inestable). 'atendio' ambiguo -> True (no castigar); el resto solo si es True.
    atendio = _as_bool(raw.get("atendio_el_motivo"))
    atendio = True if atendio is None else atendio
    extra = _as_bool(raw.get("hizo_accion_extra")) is True
    cortesia_destacada = _as_bool(raw.get("cortesia_destacada")) is True
    maltrato = _as_bool(raw.get("hubo_maltrato_grave")) is True
    # HECHOS del MODULADOR (claridad + reinsistencia). Ausente/invalido -> 'dudoso' (neutral).
    claridad = raw.get("claridad")
    if claridad not in CLARIDAD_VALS:
        claridad = "dudoso"
    cliente_reinsistio = _as_bool(raw.get("cliente_reinsistio")) is True

    # OVERRIDES deterministas de los HECHOS (la senal dura le gana al modelo):
    resolved = operator_resolved(target_messages)   # confirmó o mandó media (comprobante/KYC/tutorial)
    pushed = operator_pushed(target_messages)        # empuje concreto: link, invitación, bono por recarga
    asked = client_asked_question(target_messages)
    reasked = client_reasked(target_messages)
    # MODULADOR (calidad del piso): fricción determinista y claridad efectiva. La resolución
    # determinista PROTEGE el piso -> un 'confuso' difuso no baja una transacción confirmada,
    # y la fricción solo demota cuando el operador NO resolvió (lo determinista gana).
    # LAS DOS REINSISTENCIAS SE SUMAN. `reasked` ve el RELOJ (4+ mensajes con silencio real
    # del operador, medido con timestamps) y es ciega al CONTENIDO; `cliente_reinsistio` lo
    # LEE el modelo, y ve al cliente repitiendo el pedido con otras palabras -- insistir sin
    # necesidad de una rafaga. Antes el campo del LLM se guardaba en `dimensions` y no
    # alimentaba nada que pudiera demotar: solo entraba en `confuso_corroborado`, que no hace
    # nada cuando la claridad es 'dudoso' (el valor modal, y el que se asume por omision).
    # MEDIDO el 2026-08-13 sobre el rescore v13: 87 filas con `cliente_reinsistio=true` y
    # `friccion=false`, **71 de ellas (81,6%) en 4 y 5 estrellas**. Una de 5 estrellas se
    # desmentia sola en su rationale: "no ofrecio una solucion alternativa ni escalo el caso
    # cuando el cliente insistio en que ya llevaba 10 minutos esperando".
    # `not resolved` NO se toca: si el operador confirmo o mando el comprobante, la operacion
    # se completo y la insistencia no convierte el trabajo en deficiente. Lo determinista gana.
    # EL CLIENTE DICIENDO QUE SE RESOLVIO LE GANA A TODO. Es ground truth del unico que sabe
    # si su problema se arreglo, y apaga la friccion por la misma razon por la que ya la apaga
    # `resolved`: una insistencia que TERMINO BIEN no convierte el trabajo en deficiente.
    # Caso `060725b4`: operador que contesta en 0,2 y 0,4 minutos, cliente que cierra con
    # "Si ya me salio. Todo bien. Muy amable." -- y sacaba 1 estrella.
    confirmo_el_cliente = cliente_confirmo_resuelto(target_messages)
    if motivo == "registro":
        from src.registro import operador_dijo_que_ya_tenia_cuenta

        rechazo_de_alta = operador_dijo_que_ya_tenia_cuenta(target_messages)
    else:
        rechazo_de_alta = False
    # `cliente_reinsistio` SE RETIRO DE LA NOTA el 2026-08-14 (ver el changelog de v16 en
    # src/store.py). La friccion vuelve a ser lo que `client_reasked` mide con timestamps:
    # el cliente escribio varias veces y el operador TUVO TIEMPO de contestar y no lo hizo.
    friccion = reasked and not resolved
    # Gate 1: neutralizar 'confuso' cuando el operador resolvió determinista, o cuando el
    # cliente no preguntó nada ni reinsistió (no había nada que aclarar) -> a 'dudoso'.
    neutraliza_confuso = resolved or (not asked and not reasked)
    claridad_eff = "dudoso" if (neutraliza_confuso and claridad == "confuso") else claridad
    # Gate 2: el 'confuso' solo baja duro a 2★ si está CORROBORADO: el cliente reinsistió
    # (determinista o señal LLM cliente_reinsistio), o es un esquive genuino (preguntó y el
    # operador ni resolvió ni empujó).
    # SIN `cliente_reinsistio` (retirado el 2026-08-14): queda el silencio medido o el esquive
    # genuino. Ese segundo camino es JUSTO el instrumento para "la respuesta no contesto" --
    # el cliente pregunto y el operador ni resolvio ni empujo-- que es lo que la señal
    # retirada intentaba capturar por un proxy mucho mas ruidoso.
    confuso_corroborado = reasked or (asked and not resolved and not pushed)
    override = False
    # PIEZA 1 - PISO: el operador atendió el motivo de forma determinista (corrige la dureza
    # residual del flujo de anuncio en 'datos', donde el LLM exigía respuesta literal).
    if not atendio and (
        (motivo in _RESOLVED_FLOOR and resolved)
        or (motivo in _FUNNEL_FLOOR and (resolved or pushed))
        # info SIN consulta contestable (cliente solo saludó/agradeció/abandonó): el piso se
        # cumple respondiendo cordial -> no es deficiente (trampa abandono/sin-necesidad).
        # Solo si el cliente NO preguntó nada: si preguntó y el operador evadió, sigue deficiente.
        or (motivo == "info" and not client_asked_question(target_messages))
        # EL PISO QUE LE FALTABA A `problema`, y que vale para todos los motivos. Es el
        # unico motivo sin rubrica determinista ("'problema' NO se floorea determinista...
        # se deja al modelo"), asi que un `atendio=False` alucinado no lo corregia nada y
        # con friccion encima caia a 'mala'. La confirmacion del CLIENTE es la evidencia
        # mas dura disponible: si dijo que se resolvio, el motivo se atendio.
        or confirmo_el_cliente
        # EL ALTA ERA IMPOSIBLE: el cliente YA TENIA cuenta y el operador se lo dijo. La rama
        # del rechazo existe desde v8 pero vive SOLO en la rubrica determinista, y una sesion
        # sin traspaso de datos nunca llega ahi (`es_transaccion` da False) -- asi que el
        # camino LLM la juzgaba por un alta que no se podia hacer.
        # CASO REAL `9f0f0717` (traido por el negocio el 2026-08-14): "como se puede inscribir
        # confirmen" -> "ya tienes una cuenta amigo" -> 1 ESTRELLA con "no ofreció ni guio el
        # proceso de registro". El operador hizo exactamente lo que correspondia.
        # MEDIDO: 19 de 2.451 filas del camino LLM de `registro` (0,8%).
        or rechazo_de_alta
    ):
        atendio, override = True, True
    # 'mala' solo con maltrato real: el modelo lo sobre-marca y el maltrato del operador es
    # rarisimo; sin evidencia determinista, se descarta el maltrato -> no cae a 'mala'.
    if maltrato and not operator_maltrato(target_messages):
        maltrato, override = False, True
    # PIEZA 6 - LA PELOTA DE VUELTA AL CLIENTE ES UNA FALLA DEL PISO, NO UN TECHO.
    # Es el falso POSITIVO que la PIEZA 1 no puede atrapar por ser asimetrica (ver su nota):
    # el LLM auto-reporta `atendio_el_motivo` y nadie lo corrobora. Aca SI hay señal dura —
    # el cliente declaro que queria registrarse y el operador le devolvio la decision sin
    # actuar (src/registro.le_devolvio_la_pelota) — asi que el piso "guia el alta paso a
    # paso" NO se cumplio y la nota es 'deficiente'. Criterio del negocio del 2026-08-11
    # sobre un chat concreto: "si el cliente ya dice que quiere registrarse, y el operador le
    # pregunta, es una deficiencia".
    # Va ANTES de label_from_facts a proposito: los techos (PIEZAS 2/3/4) solo bajan hasta
    # 'aceptable', y esto no es "le falto algo", es que no hizo lo unico que habia que hacer.
    if motivo == "registro":
        from src.registro import le_devolvio_la_pelota

        if le_devolvio_la_pelota(target_messages):
            atendio, override = False, True

    label = label_from_facts(
        atendio_motivo=atendio, hizo_accion_extra=extra,
        cortesia_destacada=cortesia_destacada, hubo_maltrato_grave=maltrato,
        claridad=claridad_eff, friccion=friccion,
        confuso_corroborado=confuso_corroborado,
    )
    # el confuso solo "ajustó" la nota si de verdad la demotó (corroborado); un
    # confuso rescatado (no corroborado) no debe marcarse como override.
    if friccion or (atendio and claridad_eff == "confuso" and confuso_corroborado):
        override = True  # el modulador bajó la nota -> marca el ajuste determinista
    # PIEZA 3 - TECHO DE `registro` EN EL FALL-THROUGH. Llegar hasta aca con motivo
    # 'registro' PRUEBA que score_registro devolvio None (ver arriba), o sea que la sesion
    # NO fue una transaccion: el alta no se cerro. Y el mejor escenario de la rubrica de
    # registro es, textual, "cierra el alta y encamina el primer deposito" (src/rubrics.py)
    # -> 'excelente' es INALCANZABLE en este camino por construccion. Sin este techo el
    # fall-through podia entregar una etiqueta que su propia rubrica define como imposible.
    #
    # MEDIDO el 2026-08-07 sobre la copia de prod: 3 de las 6 filas de `registro` salieron
    # con 5 estrellas y un rationale que las desmentia ("no guio paso a paso ni proporciono
    # el link de registro"); el cliente pregunto como activar su cuenta y se quedo sin
    # activarla. Un `deposito` que respondio en 1 min pero no confirmo la acreditacion
    # sacaba 2: el ranking quedaba invertido justo donde mas importa.
    #
    # POR QUE PASABA: la PIEZA 1 es ASIMETRICA. `_FUNNEL_FLOOR` corre solo `if not atendio`,
    # asi que sabe rescatar un falso NEGATIVO del modelo pero no atrapar un falso POSITIVO.
    # `atendio_el_motivo` es el hecho que define el piso de toda la escala y en el
    # fall-through lo auto-reporta el LLM, sin nada que lo corrobore. Encima el modelo dijo
    # "claridad dudosa" en las tres, y 'dudoso' es NEUTRAL por diseño: ni baja ni bloquea el
    # uplift. Este techo es la mitad que faltaba, apoyado en las señales duras que ya se
    # calcularon arriba (`resolved` / `pushed`).
    # CORRECCION del 2026-08-07: el techo NO aplica si el cliente ABANDONO tras un pedido.
    # Tal como lo escribi a la mañana era ciego a POR QUE no se cerro el alta: capeaba igual
    # al operador que se zafo y al que ofrecio crear la cuenta y se quedo esperando una
    # respuesta que nunca llego. El segundo hizo lo que podia; lo mejorable va en la
    # recomendacion, no en la nota. El caso lo trajo el negocio con un chat concreto.
    # PIEZA 4 - NUNCA PIDIO LOS DATOS. El cliente pidio registrarse, siguio escribiendo, y
    # el operador nunca pidio los datos ni ofrecio crear la cuenta. Medido el 2026-08-07:
    # pasa en el 38,1% de los pedidos explicitos, y con el cliente presente en 510 casos
    # (52,5% de esos). Sin pedido el alta cierra 12,8% contra ~40% cuando se pide. Es lo
    # unico que el cliente pidio y no se hizo, con el cliente ahi -> techo en aceptable.
    # OJO: esto NO es la hipotesis de la verbosidad, que se midio y se descarto (ver
    # src/registro.py). Va ANTES del techo generico porque es la señal mas fuerte.
    if motivo == "registro":
        from src.registro import fue_al_punto, nunca_pidio_los_datos

        if nunca_pidio_los_datos(target_messages) and label in (
                "buena", "excelente", "aceptable"):
            label, override = "aceptable", True
    # EL ABANDONO EXIME DEL CASTIGO, NO DEL HECHO (corregido el 2026-08-13). La exencion del
    # 2026-08-07 desactivaba el techo ENTERO, y el techo tiene DOS mitades que no son lo mismo:
    #   - bajar a 'aceptable' cuando no hay señal dura es un JUICIO sobre por que no se cerro
    #     el alta, y ahi la exencion es correcta: el operador "que ofrecio crear la cuenta y se
    #     quedo esperando una respuesta que nunca llego hizo lo que podia". Eso NO se toca.
    #   - bajar 'excelente' a 'buena' es un HECHO: llegar hasta aca con motivo `registro` prueba
    #     que `score_registro` devolvio None, o sea que el alta NO se cerro, y el mejor escenario
    #     de la rubrica es textualmente "cierra el alta y encamina el primer deposito". Que el
    #     cliente se haya ido no cierra el alta.
    # MEDIDO el 2026-08-13: **45 filas de `registro` por el camino LLM con `cliente_abandono=true`
    # llegaron a 5 estrellas**, contra **0 de las 2.061 con abandono=false**. Cuatro son una
    # campaña de broadcast con este rationale y `rating_label='excelente'`: "atendió el motivo de
    # registro al explicar el proceso, PERO NO GUIO AL CLIENTE PASO A PASO NI LE PIDIO LOS DATOS
    # NECESARIOS PARA CREAR LA CUENTA". La nota maxima con el texto que la desmiente.
    # POR QUE NO ALCANZABA CON EXIGIR SEÑAL DURA: 41 de esas 45 tienen `pushed=True` (mencionan
    # el bono o mandan el link), asi que un guard sobre `resolved or pushed` habria atrapado 1.
    # Lo universal es otro hecho: `se_creo_la_cuenta` da False en las 45.
    if motivo == "registro":
        if not abandono and not (resolved or pushed):
            # EL JUICIO, y de esto SI exime el abandono. Ni link/invitacion concreta ni
            # entrega: el piso de la rubrica -"guia el alta de la cuenta paso a paso"- no
            # esta corroborado por NINGUNA señal dura, el 'atendio' es solo palabra del
            # modelo. Techo en aceptable (falto algo), no castigo.
            if label in ("buena", "excelente"):
                label, override = "aceptable", True
        elif label == "excelente" and not (abandono and fue_al_punto(target_messages)):
            # EL ALTA NO SE CERRO, y llegar hasta aca lo prueba. El mejor escenario de la
            # rubrica es textualmente "cierra el alta y encamina el primer deposito".
            # LA EXENCION DEL 2026-08-07 SE CONSERVA, PERO ACOTADA A QUIEN SE OFRECIO: esa
            # decision protege al operador que "ofrecio crear la cuenta y se quedo esperando
            # una respuesta que nunca llego -- hizo lo que podia". El que solo recito la
            # plantilla de venta no hizo lo que podia.
            # MEDIDO el 2026-08-13 sobre las 45 filas en 5 estrellas con abandono: **37
            # ofrecieron de verdad y conservan su nota; 8 no ofrecieron nada**. (La primera
            # medicion daba 41 y 4, por el falso positivo de `_AL_PUNTO_RE` con "te registras"
            # -- ver src/registro.py.)
            label, override = "buena", True
    # PIEZA 5 - UN 5 NO CONVIVE CON UN ERROR DETECTADO. Si el modelo listo algo que falto, la
    # sesion no fue el MEJOR ESCENARIO, que es lo que significa la nota maxima en la escala v4.
    # MEDIDO el 2026-08-07 sobre 769 sesiones con 5 estrellas: las 612 deterministas salen
    # limpias por construccion, pero de las 157 del camino LLM **33 (21%) listaban errores al
    # lado del 5** ("no se aclaro por que los giros aun no se activaban").
    # Se elige BAJAR LA NOTA y no borrar el texto: el error listado suele ser real, y taparlo
    # para salvar el 5 esconderia informacion util. Es la decision inversa a la del guard de
    # `aciertos`, donde el texto SI se descarta — ahi el problema era presentar un reproche
    # como logro; aca el problema es una nota que el propio texto desmiente.
    errores_reportados = [e for e in (raw.get("dimensions") or {}).get("errores") or []
                          if isinstance(e, str) and e.strip()]
    if label == "excelente" and errores_reportados:
        label, override = "buena", True
    # PIEZA 7 - TECHO DEL FALL-THROUGH TRANSACCIONAL. Llegar hasta aca con motivo
    # `deposito` o `retiro` PRUEBA que su rubrica determinista devolvio None (ver arriba):
    # el gate no encontro la transaccion. En `deposito` eso significa que NO HAY
    # COMPROBANTE del cliente, y el comprobante -dice el docstring de
    # `deposito.es_transaccion`- "se exige por AUDITORIA". Si encima el operador AFIRMA que
    # la plata entro, la sesion no puede valer el MEJOR ESCENARIO del motivo: la nota maxima
    # de esa rubrica es una acreditacion confirmada Y verificable, y aca no hay nada que
    # verificar. Se baja a 'buena' ("se hizo bien"), no mas: la plata pudo haber entrado de
    # verdad fuera de WhatsApp, y castigar mas seria inventar en la direccion contraria.
    #
    # MEDIDO el 2026-08-13 sobre el rescore v13: el camino determinista de `deposito` da 5
    # estrellas en 12 de 1.822 filas (0,7%); el fall-through, en 102 de 208 (49,5%). Setenta
    # veces mas. Es la MISMA enfermedad que `es_transaccion` ya midio y cerro para los
    # depositos CON comprobante que caian aca (5 estrellas el 68,2% de las veces contra el
    # 3,6% de las transacciones); esto cierra la mitad que quedaba.
    #
    # EL TECHO ES QUIRURGICO A PROPOSITO, y esto es lo que evita que sea un exceso: de esas
    # 102 filas en 5 estrellas, solo 23 (22,5%) afirman una acreditacion. Las otras 79 son
    # CONSULTAS ("¿como recargo?") bien atendidas, y ahi el 5 es legitimo -- contestar bien
    # es el mejor escenario disponible de una consulta. Un techo plano habria demotado a las
    # 79 por un problema que no tienen.
    #
    # ASIMETRIA DE `retiro`: ahi el comprobante lo manda el OPERADOR, asi que su media ES la
    # entrega y respalda la afirmacion -> no se aplica el techo.
    if motivo in ("deposito", "retiro") and label == "excelente":
        afirma = operator_acreditacion(target_messages)
        respaldado = motivo == "retiro" and operator_sent_media(target_messages)
        if afirma and not respaldado:
            label, override = "buena", True
    # UN MERITO NECESITA UN AUTOR. Si ningun mensaje del negocio tiene una persona detras
    # -- chatbot, marketing por api, o sin remitente y sin user_id-- no hay atencion de
    # operador que premiar, por mas que el modelo la describa.
    # CASO REAL `c1034a14`: la sesion entera es un menu de chatbot ("/start" -> "Panita como
    # te ayudo hoy 😎 1. Recargar" -> "1") y salio con **5 ESTRELLAS** por "el operador
    # atendió el motivo del depósito al confirmar la operación con una respuesta implícita".
    # El LLM le atribuyo al operador lo que hizo el bot.
    # SE TOPA, NO SE SALTEA: `1bd61c16` es una persona que escribio 13 veces y solo le
    # contesto un bot, y ESE 1 estrella es un problema real que saltear esconderia.
    # MEDIDO: de 16.896 sesiones evaluadas, 4 tienen como unico "operador" mensajes sin
    # persona detras y 1 llego a 4-5 estrellas.
    sin_nadie = not hay_persona_del_negocio(target_messages)
    if sin_nadie and label in ("buena", "excelente"):
        label, override = "aceptable", True
    stars = label_to_stars(motivo, label)
    rationale = raw.get("rating_rationale", "")
    if sin_nadie:
        rationale = (f"{rationale} [ajuste determinista de hechos: no hubo ningún operador "
                     "detrás de esta conversación]")
    if override:
        rationale = f"[ajuste determinista de hechos] {rationale}"
    # EL TEXTO NO PUEDE DESMENTIR A UNA SEÑAL DURA. El modelo escribe "no se pidieron los
    # datos" sobre sesiones donde `fue_al_punto` prueba que SI se pidieron, y el operador lee
    # esa acusacion pegada a una nota que dice que hizo bien el trabajo. La ESTRELLA esta
    # bien (la protege el piso determinista); lo que miente es el texto.
    # MEDIDO el 2026-08-14 sobre v15: 149 de 283 reclamos (52,7%) son falsos. Las otras 134
    # son ciertas, asi que NO se filtra el texto -- se conserva entero y se le anexa la
    # correccion, para que quien lo lee vea las dos cosas y sepa cual manda.
    # Y si el alta era IMPOSIBLE, el texto tiene que decirlo: sin esto el operador lee "no
    # ofreció ni guio el proceso de registro" sobre una cuenta que ya existia.
    if rechazo_de_alta:
        rationale = (f"{rationale} [ajuste determinista de hechos: el cliente ya tenía una "
                     "cuenta, así que no había alta que hacer]")
    if motivo == "registro":
        from src.registro import rationale_desmiente_el_pedido

        if rationale_desmiente_el_pedido(rationale, target_messages):
            rationale = (f"{rationale} [ajuste determinista de hechos: el operador SI "
                         "pidio los datos del alta]")

    # ATENCION (#5 + señal de resolucion). Si el operador empujo (link/invitacion/bono por
    # recarga) es 'empujo' aunque el LLM lo subvalue; si no, 'no_respondio' es falso cuando
    # el operador confirmo o mando el comprobante -> al menos 'pasivo'.
    atencion = raw.get("atencion")
    if atencion not in schema["properties"]["atencion"]["enum"]:
        atencion = None
    if pushed:
        if atencion in ("pasivo", "no_respondio", None):
            atencion = "empujo"
    elif atencion == "no_respondio" and resolved:
        atencion = "pasivo"

    # RECOMENDACION: por defecto la del pase de scoring; si hay un RECOMMENDER (subagente
    # dedicado de coaching, opcional) se usa esa, que corre con su prompt propio (± ejemplos).
    recomendacion = raw.get("recomendacion") or ""
    if recommender is not None:
        try:
            recomendacion = recommender(target_messages, motivo, label) or recomendacion
        except Exception:  # noqa: BLE001 - una falla del coach no debe tumbar el score
            pass
    # EL 5 NO LLEVA CONSEJO CORRECTIVO, TAMPOCO EN ESTE CAMINO. Las seis rubricas
    # deterministas ya devuelven "" en 5 estrellas (`deposito._coaching`, `retiro._coaching`,
    # `registro:317`, `promo:180`, `info:137`); el pase con LLM era el unico que no, porque el
    # texto lo escribe el modelo y el prompt le pide devolver "" "solo si ya fue excelente y
    # no aplica ninguna regla" -- y las reglas por motivo aplican casi siempre, asi que en la
    # practica nunca devolvia vacio.
    # MEDIDO el 2026-08-13 sobre el rescore v13: 623 de 4.782 filas en 5 estrellas (13,0%)
    # traian consejo correctivo, y eran el 100% de los 5 de este camino (439 de 439).
    # 'excelente' significa EL MEJOR ESCENARIO del motivo: un reproche al lado es la misma
    # contradiccion que la PIEZA 5 arregla en la nota, pero en el campo que la persona LEE.
    # Los fragmentos deterministas de `refine_recomendacion` NO se tocan y siguen llegando
    # (ver abajo): el aviso de cambiar la contraseña no es un reproche al operador, es una
    # instruccion para el CLIENTE, y se agrego a proposito el 2026-08-12 porque no disparaba
    # justo donde mas aplica.
    if label == "excelente":
        recomendacion = ""
    # Capa 1: fragmentos deterministas de alto valor (el LLM casi nunca los produce)
    # anteponen coaching aspiracional; nunca afectan la nota.
    recomendacion = refine_recomendacion(recomendacion, motivo=motivo, target_messages=target_messages)

    # EL "POR QUE" BIDIRECCIONAL. aciertos[] = lo que se hizo bien (derivado de hechos,
    # con la nota del LLM como evidencia); errores[] = lo del LLM + el porqué determinista
    # de la baja (fricción / confuso), asi tanto subir como bajar tienen su motivo explicito.
    aciertos = derive_aciertos(
        atendio_motivo=atendio, hizo_accion_extra=extra,
        cortesia_destacada=cortesia_destacada, claridad=claridad_eff,
        friccion=friccion, dimensions=raw.get("dimensions") or {},
    )
    dims_out = dict(raw.get("dimensions") or {})
    errores = list(dims_out.get("errores") or [])
    # EL TEXTO DICE LA VERDAD SOBRE SU ORIGEN. `friccion` se arma con dos señales que no
    # prueban lo mismo: `reasked` mide SILENCIO REAL con timestamps, `cliente_reinsistio` es
    # juicio libre del modelo y no exige que el operador haya callado. El `or` de v14 se
    # conserva -esa demotacion es la decision tomada-, pero el error mostrado no puede
    # acusar de "no respondio" a quien respondio.
    # MEDIDO el 2026-08-14 sobre v15: de las 57 filas con friccion en el camino LLM, **36
    # (63,2%) tienen `client_reasked()=False`** -- 32 en 2 estrellas y 4 en 1. En el caso
    # `43df99b7` el operador contesto CADA UNO de los ~10 mensajes del cliente.
    # EL TEXTO YA NO PUEDE MENTIR SOBRE SU ORIGEN. Cuando `cliente_reinsistio` alimentaba la
    # friccion hacia falta una rama por origen, porque 36 de 57 filas decian "sin respuesta
    # del operador" sobre operadores que habian contestado todo. Retirada esa señal,
    # `friccion` implica `reasked` -- silencio medido con timestamps-- y la frase es cierta
    # por construccion.
    if friccion:
        errores.append("El cliente tuvo que reinsistir sin respuesta del operador.")
    if atendio and claridad_eff == "confuso" and confuso_corroborado:
        # solo si el confuso realmente demotó (corroborado); un confuso rescatado
        # (no corroborado) no debe arrastrar un error duro en el "por qué".
        errores.append("La respuesta no fue clara: el cliente tuvo que inferir.")
    dims_out["errores"] = errores
    dims_out["aciertos"] = aciertos
    dims_out["claridad"] = claridad_eff
    # SE SIGUE PERSISTIENDO PERO YA NO CALIFICA NI SE MUESTRA (2026-08-14). Queda como dato
    # crudo del modelo para poder volver a medirlo si alguna vez se consigue un instrumento
    # que lo detecte: hoy acierta 14 de 103 y el fenomeno real es el 0,3% del padron.
    dims_out["cliente_reinsistio"] = cliente_reinsistio
    # Se persiste para que el FRONT pueda decir "el cliente no contesto mas" en vez de
    # dejar al que mira adivinando por que el tramite quedo abierto. Va en `dimensions`
    # (jsonb) a proposito: no necesita migracion de schema.
    dims_out["cliente_abandono"] = abandono
    dims_out["friccion"] = friccion

    return ScoreResult(
        rubric=motivo,
        motivo=motivo,
        dimensions=dims_out,
        rating_label=label,
        rating_rationale=rationale,
        stars=stars,
        llm_model=llm.model,
        atencion=atencion,
        deposit_observed=_as_bool(raw.get("deposit_observed")),
        floor_applied=override,
        recomendacion=recomendacion,
        claridad=claridad_eff,
        friccion=friccion,
        aciertos=aciertos,
    )
