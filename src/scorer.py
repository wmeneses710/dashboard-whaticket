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

from src.prompts import build_motivo_prompt, build_motivo_schema
from src.recommendations import refine_recomendacion
from src.rubrics import MOTIVOS, derive_aciertos, label_from_facts, label_to_stars
from src.signals import (
    cliente_abandono_tras_pedido,
    operator_confirmation,
    operator_maltrato,
    operator_pushed,
    operator_resolved,
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
    friccion = reasked and not resolved
    # Gate 1: neutralizar 'confuso' cuando el operador resolvió determinista, o cuando el
    # cliente no preguntó nada ni reinsistió (no había nada que aclarar) -> a 'dudoso'.
    neutraliza_confuso = resolved or (not asked and not reasked)
    claridad_eff = "dudoso" if (neutraliza_confuso and claridad == "confuso") else claridad
    # Gate 2: el 'confuso' solo baja duro a 2★ si está CORROBORADO: el cliente reinsistió
    # (determinista o señal LLM cliente_reinsistio), o es un esquive genuino (preguntó y el
    # operador ni resolvió ni empujó).
    confuso_corroborado = reasked or cliente_reinsistio or (asked and not resolved and not pushed)
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
        from src.registro import nunca_pidio_los_datos

        if nunca_pidio_los_datos(target_messages) and label in (
                "buena", "excelente", "aceptable"):
            label, override = "aceptable", True
    if motivo == "registro" and not abandono:
        if not (resolved or pushed):
            # Ni link/invitacion concreta ni entrega: el piso de la rubrica -"guia el alta
            # de la cuenta paso a paso"- no esta corroborado por NINGUNA señal dura, el
            # 'atendio' es solo palabra del modelo. Techo en aceptable (falto algo), no
            # castigo: bajarlo mas seria inventar en la direccion contraria.
            if label in ("buena", "excelente"):
                label, override = "aceptable", True
        elif label == "excelente":
            # Guio de verdad (hay empuje o entrega) pero el alta no se cerro -> "se hizo
            # bien" (4), que es lo que efectivamente paso, no el mejor escenario.
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
    stars = label_to_stars(motivo, label)
    rationale = raw.get("rating_rationale", "")
    if override:
        rationale = f"[ajuste determinista de hechos] {rationale}"

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
    if friccion:
        errores.append("El cliente tuvo que reinsistir sin respuesta del operador.")
    if atendio and claridad_eff == "confuso" and confuso_corroborado:
        # solo si el confuso realmente demotó (corroborado); un confuso rescatado
        # (no corroborado) no debe arrastrar un error duro en el "por qué".
        errores.append("La respuesta no fue clara: el cliente tuvo que inferir.")
    dims_out["errores"] = errores
    dims_out["aciertos"] = aciertos
    dims_out["claridad"] = claridad_eff
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
