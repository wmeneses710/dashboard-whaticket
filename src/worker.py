"""Worker de scoring: puntua conversaciones PENDIENTES de una cuenta.

Reutilizable por el batch manual (scripts/run_scoring.py) y por el loop en
background del contenedor (src/app.py). Idempotente: solo toma conversaciones
que todavia no estan en conversation_scores. Scopeado por cuenta: datos y
sistemas conviven en la misma BD y el worker procesa las cuentas configuradas.
"""
from __future__ import annotations

import time
import traceback

from src.agilidad import score_agilidad
from src.context import fetch_messages, fetch_session_messages, fetch_thread_context
from src.deposito import es_transaccion as es_transaccion_deposito
from src.deposito import score_deposito
from src.deposito import interaccion_juzgada as interaccion_juzgada_deposito
from src.deposits import deposit_candidate_count
from src.interacciones import tiempos_de
from src.registro import interaccion_juzgada as interaccion_juzgada_registro
from src.info import score_info
from src.motivo_de_agente import motivo_de_agente
from src.retiro import interaccion_juzgada as interaccion_juzgada_retiro
from src.retiro import score_retiro
from src.llm import OllamaClient
from src.metrics import message_stats, primary_operator, reparto_por_interaccion
from src.operators import build_operator_map, nombre_de_notas, operator_name
from src.redireccion import (
    build_lineas_map,
    respuesta_fue_solo_traspaso,
    tail_de,
    traspaso_limpio,
    score_redireccion,
)
from src.signals import cliente_abandono_tras_pedido, desenlace_del_cliente
from src.signals import client_sin_motivo
from src.sin_respuesta import score_sin_respuesta
from src.solo_cortesia import score_solo_cortesia
from src.router import decide_eligibility, decide_rubric
from src.segments import es_grupo_de_whatsapp
from src.registro import alta_abandonada_por_datos
from src.scorer import score_by_motivo
from src.segments import segment_for_queue
from src.sessions import evaluate_session
from src.store import (
    build_score_record,
    ensure_scores_columns,
    ensure_session_scoring_migration,
    upsert_score,
)

_CONV_FIELDS = """c.id, c.account, c.ticket_id, c.user_id, c.created_at,
       c.first_sent_message_at, c.resolved_at, c.is_new_contact,
       q.name AS queue_name, conn.channel AS channel, tk.is_group AS is_group,
       ct.number AS contact_number, conn.number AS linea_propia"""

PENDING_SQL = f"""
SELECT {_CONV_FIELDS}
  FROM conversations c
  LEFT JOIN queues q         ON q.id    = c.queue_id
  LEFT JOIN connections conn ON conn.id = c.connection_id
  LEFT JOIN tickets tk       ON tk.id   = c.ticket_id
  LEFT JOIN contacts ct      ON ct.id   = tk.contact_id
 WHERE c.resolved_at IS NOT NULL AND c.account = %(account)s
   AND NOT EXISTS (SELECT 1 FROM conversation_scores s WHERE s.conversation_id = c.id)
 ORDER BY c.created_at DESC
 LIMIT %(limit)s
"""


def fetch_pending(cur, account: str, limit: int) -> list[dict]:
    """Conversaciones resueltas de la cuenta que aun NO fueron scoreadas."""
    cur.execute(PENDING_SQL, {"account": account, "limit": limit})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def score_and_store(conn, conv: dict, llm, op_map: dict):
    """Scorea UNA conversacion y la persiste. Devuelve (eval_status, skip_reason, score)."""
    with conn.cursor() as cur:
        msgs = fetch_messages(cur, conv["id"])
        ctx = fetch_thread_context(cur, conv["ticket_id"], conv["id"])
    stats = message_stats(msgs)
    deposit_count = deposit_candidate_count(msgs)  # gate determinista (independiente del eval_status)
    operator_id = primary_operator(msgs)
    # QUINTA PUERTA: si no hay user_id ni firma, el nombre vive en las NOTAS del CRM
    # ("<Nombre> *resuelto* la conversación"), que ya leemos para cortar interacciones.
    # Rescata 880 de las 881 sesiones sin nombre, con 99% de acierto. Ver src/operators.py.
    # SEXTA y ultima: la ASIGNACION del CRM (`conversations.user_id`, FK real a `users`).
    # Va ultima porque apunta a quien TIENE la conversacion -- se transfiere -- y no a quien
    # la trabajo: medida contra la verdad conocida acierta el 91%, contra el 99% de la nota.
    # Cierra el hueco exacto: de 882 sesiones sin user_id ni firma, la nota rescata 860 y la
    # asignacion nombra a los 22 que quedan.
    # La fila de ENTRADA de este path se llama `conv` (en el path por sesion es `sess`):
    # copiar la puerta sin renombrar dejaba un NameError latente, que solo se disparaba
    # cuando fallaban las tres puertas previas. Ver tests/test_worker.py.
    op_name = ((op_map.get(str(operator_id)) if operator_id else None)
               or operator_name(msgs, operator_id)
               or nombre_de_notas(msgs)
               or (op_map.get(str(conv["user_id"])) if conv.get("user_id") else None))
    rubric = decide_rubric(
        operator_message_count=stats.operator_message_count,
        bot_message_count=stats.bot_message_count,
    )
    eval_status, skip_reason = decide_eligibility(
        real_message_count=stats.message_count,
        customer_message_count=stats.contact_message_count,
        business_message_count=stats.operator_message_count + stats.bot_message_count,
        customer_text_count=stats.contact_text_message_count,
    )
    score = None
    if eval_status == "evaluated":
        # Unificado con el path de sesión: el LLM clasifica el MOTIVO y califica en 2
        # capas (score_by_motivo). rubric (human/bot) queda solo para la columna legacy.
        score = score_by_motivo(
            target_messages=msgs, thread_context=ctx, llm=llm, deposit_hint=deposit_count > 0
        )
    record = build_score_record(
        conversation=conv, stats=stats, rubric=rubric,
        eval_status=eval_status, skip_reason=skip_reason, score=score,
        operator_id=operator_id, operator_name=op_name, deposit_count=deposit_count,
    )
    with conn.cursor() as cur:
        upsert_score(cur, record)
    conn.commit()
    return eval_status, skip_reason, score


def score_batch(conn, llm, account: str, limit: int, op_map: dict | None = None) -> dict:
    """Scorea un lote de pendientes de una cuenta. Devuelve conteos."""
    if op_map is None:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)
    with conn.cursor() as cur:
        pending = fetch_pending(cur, account, limit)
    counts = {"evaluated": 0, "skipped": 0, "error": 0, "seen": len(pending)}
    for conv in pending:
        try:
            eval_status, _, _ = score_and_store(conn, conv, llm, op_map)
            counts[eval_status] += 1
        except Exception as e:  # noqa: BLE001 - no abortar el lote por una conversacion
            # rollback: si el fallo fue DB-side, la txn queda abortada y cascadearia
            # al resto del lote (InFailedSqlTransaction) sin este reset.
            conn.rollback()
            counts["error"] += 1
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [worker] error conv "
                  f"{conv.get('conversation_id') or conv.get('id')} ({account}): "
                  f"{type(e).__name__}: {str(e)[:300]}", flush=True)
            print(traceback.format_exc()[-1500:], flush=True)
    return counts


# --- Scoring por SESION -----------------------------------------------------------
# Espeja el path por-conversacion (PENDING_SQL/fetch_pending/score_and_store/
# score_batch) pero a grano SESION. DECISION A: una sesion se scorea solo cuando
# CERRO = su ultimo episodio quedo atras hace mas de 6h (end_at < now() - 6h). La
# fila resultante queda keyeada por conversation_id = session_id (la conversacion de
# ENTRADA, el primer episodio de la sesion) con la columna session_id seteada.
# run_worker_loop YA usa este path (el flip se hizo). El path por-conversacion
# (PENDING_SQL/fetch_pending/score_and_store/score_batch) queda como API para el batch
# manual (scripts/), pero el loop del contenedor scorea por sesion.
PENDING_SESSIONS_SQL = f"""
SELECT {_CONV_FIELDS}, cs.session_id AS session_id
  FROM conversation_sessions cs
  JOIN conversations c       ON c.id    = cs.session_id
  LEFT JOIN queues q         ON q.id    = c.queue_id
  LEFT JOIN connections conn ON conn.id = c.connection_id
  -- `tickets.is_group`: un grupo de WhatsApp no es una atencion uno-a-uno y no se
  -- califica (src/router.decide_eligibility). LEFT y no JOIN duro porque 70.880 de las
  -- 139.708 sesiones pendientes NO tienen fila en `tickets`: un JOIN las borraria del
  -- padron entero, que es un daño mucho mayor que el que este gate viene a arreglar.
  LEFT JOIN tickets tk       ON tk.id   = c.ticket_id
  -- `contacts.number` es el RESPALDO de `is_group`: el JID del grupo viaja ahi. Se alcanza
  -- solo por `tickets.contact_id` (`conversations` no tiene ruta directa al contacto), y
  -- va LEFT por lo mismo que el anterior: 13.052 sesiones pendientes no tienen contacto.
  LEFT JOIN contacts ct      ON ct.id   = tk.contact_id
 WHERE cs.account = %(account)s
   AND cs.end_at < now() - interval '6 hours'
   -- Pendiente = sin score, O con un score MAS VIEJO que el ultimo episodio de la
   -- sesion (la sesion crecio despues de scorearse, p. ej. una continuacion diferida
   -- que se mergeo hasta 48h despues) -> re-scorear para no quedar con nota vieja.
   AND NOT EXISTS (
     SELECT 1 FROM conversation_scores s
      WHERE s.session_id = cs.session_id AND s.scored_at >= cs.end_at)
 ORDER BY cs.end_at DESC, cs.session_id
 LIMIT %(limit)s
"""


# Motivos cuya interaccion juzgada es ACOTABLE de forma determinista (la rubrica tiene un
# ancla: el comprobante del cliente en deposito, el formulario del pedido en retiro). Los
# demas pasan por el LLM sobre la sesion entera y todavia no tienen ancla.
_ANCLA_POR_MOTIVO = {
    "deposito": interaccion_juzgada_deposito,
    "retiro": interaccion_juzgada_retiro,
    # `registro` entro el 2026-08-12: su ancla es el traspaso de datos del cliente, y ya
    # estaba en el codigo — la rubrica dice "despues de recibir los datos". Sin esto, el
    # rationale citaba minutos y al lado se persistian dias (caso `c4a69129`: 1,3 min de
    # texto contra 20.226 min de metrica).
    "registro": interaccion_juzgada_registro,
}


def fetch_pending_sessions(cur, account: str, limit: int) -> list[dict]:
    """Sesiones CERRADAS de la cuenta que aun NO fueron scoreadas por sesion.

    Trae los campos de la conversacion de ENTRADA (mismos que _CONV_FIELDS) + el
    session_id. La fila resultante alimenta score_session_and_store, que la keyea por
    conversation_id = session_id.
    """
    cur.execute(PENDING_SESSIONS_SQL, {"account": account, "limit": limit})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def score_session_and_store(conn, sess: dict, llm, op_map: dict,
                            recommender=None, lineas: dict | None = None):
    """Scorea UNA sesion (transcript mergeado) y la persiste. Devuelve (eval_status,
    skip_reason, score). Espeja score_and_store pero a grano SESION.
    recommender: sub-evaluador angosto opcional (ver src/subeval.py)."""
    with conn.cursor() as cur:
        msgs = fetch_session_messages(cur, sess["session_id"])
    stats, rubric, eval_status, skip_reason = evaluate_session(
        msgs, lineas=lineas,
        es_grupo=es_grupo_de_whatsapp(sess.get("is_group"), sess.get("contact_number")))
    deposit_count = deposit_candidate_count(msgs)  # gate determinista (indep. del eval_status)
    # El gate de LAS DOS PUERTAS, solo para reconciliar `deposit_mismatch`. `deposit_count`
    # sigue siendo el CONTADOR de volumen (lo suman los cuadros) y no se toca; lo que se
    # arregla es con QUE se compara la observacion. Ver store._deposit_mismatch.
    deposit_gate = es_transaccion_deposito(msgs)
    ventana_juzgada = None  # la interaccion que la rubrica miro, si es acotable (ver abajo)
    operator_id = primary_operator(msgs)
    # QUINTA PUERTA: si no hay user_id ni firma, el nombre vive en las NOTAS del CRM
    # ("<Nombre> *resuelto* la conversación"), que ya leemos para cortar interacciones.
    # Rescata 880 de las 881 sesiones sin nombre, con 99% de acierto. Ver src/operators.py.
    # SEXTA y ultima: la ASIGNACION del CRM (`conversations.user_id`, FK real a `users`).
    # Va ultima porque apunta a quien TIENE la conversacion -- se transfiere -- y no a quien
    # la trabajo: medida contra la verdad conocida acierta el 91%, contra el 99% de la nota.
    # Cierra el hueco exacto: de 882 sesiones sin user_id ni firma, la nota rescata 860 y la
    # asignacion nombra a los 22 que quedan.
    op_name = ((op_map.get(str(operator_id)) if operator_id else None)
               or operator_name(msgs, operator_id)
               or nombre_de_notas(msgs)
               or (op_map.get(str(sess["user_id"])) if sess.get("user_id") else None))
    score = None
    if eval_status == "evaluated":
        # NADIE CONTESTO: va PRIMERO, antes del ruteo por segmento, porque es una falla en
        # los dos y no depende del motivo -- es ANTERIOR a cualquier motivo. Determinista y
        # sin LLM: pagar una inferencia para leer una conversacion donde el negocio no
        # escribio nada es gasto puro. Hasta el 2026-08-21 esto era el skip `no_agent_reply`
        # y escondia 1.167 sesiones. Ver src/sin_respuesta.py.
        score = score_sin_respuesta(msgs)
        if score is not None:
            pass
        elif client_sin_motivo(msgs):
            # EL CLIENTE NO PLANTEO NADA: se juzga el estandar de cierre y nada mas. VA
            # DESPUES de `sin_respuesta` a proposito, y ese orden ES la prioridad del negocio
            # ("si no hubo respuesta, ese caso manda: es informacion mas util que sin_motivo").
            # Determinista y sin LLM: la señal esta verificada contra el modelo -- gemma4:12b
            # acerto 40/40 sobre una muestra mixta con control y coincidio con
            # `client_sin_motivo` en todas. Ver scripts/bench_sin_motivo.py.
            score = score_solo_cortesia(msgs, sess.get("resolved_at"))
        elif segment_for_queue(sess.get("queue_name")) == "agente":
            # AGENTE: SIEMPRE determinista, SIN LLM. Correr el pase con LLM aca aplicaria la
            # vara COMERCIAL del jugador (uplift, empujo/pasivo) y topaba el 94% de las
            # sesiones de agente en 3 estrellas por diseño. Eso no cambia.
            # LO QUE CAMBIO el 2026-08-21: el segmento pasa a tener MOTIVO. Hasta esa fecha
            # sus 61.949 filas evaluadas tenian `motivo = NULL` y las calificaba `agilidad`,
            # que mide UNICAMENTE el reloj -- y el manual le dedica un capitulo entero, con
            # una seccion literal ("Procesos que si gestionamos para agentes") que nombra
            # recargas, pagos, reclamos, diseño, servicios activos, revision de informacion
            # y apoyo operativo.
            # El motivo lo decide `motivo_de_agente` (determinista, ver ese modulo) y rutea a
            # la rubrica que corresponde; `info` absorbe las consultas propias del agente por
            # decision del negocio, porque todas son gente preguntando por algo.
            # `agilidad` SE QUEDA con el 12,9% que no tiene motivo probable: ahi no se
            # inventa uno para que la fila se vea completa.
            # EL SEGMENTO VIAJA A LA RUBRICA porque el manual trata distinto el cierre del
            # agente (ver el relevo en src/deposito.py y src/retiro.py).
            motivo_ag = motivo_de_agente(msgs)
            if motivo_ag == "deposito":
                score = score_deposito(msgs, sess.get("resolved_at"), lineas,
                                       segmento="agente")
            elif motivo_ag == "retiro":
                score = score_retiro(msgs, sess.get("resolved_at"), lineas,
                                     segmento="agente")
            elif motivo_ag == "info":
                score = score_info(msgs, sess.get("resolved_at"))
            else:
                score = score_agilidad(msgs)
            # Las rubricas transaccionales CEDEN el turno cuando no es transaccion. Medido
            # sobre 800 sesiones de agente eso no paso ni una vez, pero si pasara la sesion
            # no puede quedar sin nota ni caer al LLM: vuelve a `agilidad`.
            if score is None:
                score = score_agilidad(msgs)
        # EL MAPA Y LA LINEA PROPIA VIAJAN A LA DETECCION. Sin ellos `es_traspaso` solo mira
        # la frase, y la frase no discrimina: medidos 41 mensajes con "comuniquese ...
        # agente" + numero, 28 apuntan a una linea NUESTRA y 13 a un numero AJENO -- con la
        # MISMA redaccion. Ver src/redireccion.es_traspaso.
        elif respuesta_fue_solo_traspaso(msgs, lineas, tail_de(sess.get("linea_propia"))):
            # REDIRECCION: la respuesta del negocio fue SOLO un traspaso a otra linea.
            # Motivo determinista y SIN LLM (decision del negocio, 2026-08-20). Hasta esa
            # fecha esto era un skip; ahora lleva nota, pero sigue sin pasar por el modelo:
            # que todos los mensajes del negocio sean traspaso lo dice una funcion pura, y
            # a donde apunta lo dice `connections`. Ninguno de los dos hechos se puede
            # verificar leyendo el transcript, asi que preguntarle al modelo seria pagar una
            # inferencia para despues pisarla. Ver src/redireccion.py y src/rubrics.py
            # (MOTIVOS_DEL_LLM, que lo deja afuera del enum del prompt a proposito).
            # VA DESPUES del segmento `agente`: el ruteo por segmento es mas fundamental
            # (un agente se mide con su propio reloj) y no lo pisa un traspaso.
            # UN TRASPASO LIMPIO NO SE CALIFICA (decision del negocio, 2026-08-24): "si es
            # redireccion no deberia ni calificarse, porque es algo que no le compete, y la
            # mayoria ni explica". Medido sobre 2.500 sesiones: 13 traspasos puros, **12 con
            # 4 estrellas** -- una nota que califica igual al 92% no mide nada.
            # NO vuelve el problema que lo saco de skip el 2026-08-20 (que BORRABA el
            # traspaso del tablero): la tarjeta de sin evaluar desglosa por causa y
            # `SKIP_LABEL` ya tiene `redireccion`, asi que se sigue contando.
            # VA ACA Y NO EN `evaluate_session` porque ahi correria ANTES del chequeo de
            # cortesia, y el negocio decidio el 2026-08-07 que el bucket A se queda en
            # `sin_motivo`. La prioridad vive en ESTE orden.
            _propia = tail_de(sess.get("linea_propia"))
            if traspaso_limpio(msgs, lineas, _propia):
                eval_status, skip_reason, score = "skipped", "redireccion", None
            else:
                # Sin destino resoluble o a una linea caida: eso SI le compete al operador
                # -- el eligio a donde mandarlo y el cliente quedo sin a donde escribir.
                score = score_redireccion(msgs, lineas, _propia)
        else:
            # Pase v2: el LLM clasifica el MOTIVO y califica en 2 capas. thread_context
            # vacio: la sesion YA mergea todos los episodios del ticket. deposit_hint pasa
            # la senal determinista de comprobante para anclar el motivo 'deposito'.
            score = score_by_motivo(
                target_messages=msgs, thread_context="", llm=llm,
                deposit_hint=deposit_count > 0, recommender=recommender,
                # Cierre del ticket: lo necesita la pregunta de cierre para exigir la espera
                # de 5 min (regla del negocio). Sin el, el credito se mantiene.
                cierre_at=sess.get("resolved_at"),
                # El mapa de lineas ya se construye para el skip por traspaso; la rubrica
                # transaccional lo necesita para distinguir "derivo al AGENTE del cliente"
                # (numero ajeno, procedimiento correcto del manual) de "derivo a otra linea
                # NUESTRA" (que es `redireccion` y tiene su propia regla).
                lineas=lineas,
            )
    # REGISTRO CON LOS DATOS A MEDIAS: no se evalua. El operador pidio los datos y el
    # cliente no los mando enteros, asi que no hay nada que juzgarle. Decision del
    # negocio (2026-08-25): "si no lo hizo en este chat contaria como abandono, porque
    # el usuario no completo los requisitos aunque se le pidieron".
    #
    # VA ACA Y NO EN `score_by_motivo` porque es el PRIMER skip que depende del MOTIVO, y
    # el motivo lo elige el LLM: hasta hoy todos se decidian antes de la inferencia. Este
    # archivo ya es donde viven los skips (ver el de `redireccion` mas arriba), y meterlo
    # en el scorer obligaba a cambiarle la firma -- 47 tests y 10 monkeypatches de churn
    # para no reusar el idioma que ya estaba.
    #
    # SEPARA, NO VACIA: de las 18 filas de la copia que cobran 2 estrellas por "el alta
    # quedo a medias", 11 caen aca y 7 SE QUEDAN con su nota, porque ahi el cliente si
    # mando todo y el que no entrego fue el operador. Ver registro.datos_completos_del_alta.
    if score is not None and score.motivo == "registro" and alta_abandonada_por_datos(msgs):
        eval_status, skip_reason, score = "skipped", "datos_incompletos", None
    # EL ACOTADO VALE PARA TODOS LOS CAMINOS, no solo para el del jugador. Hasta el
    # 2026-08-24 esta consulta vivia DENTRO del `else` de arriba, asi que en el segmento
    # `agente` `ventana_juzgada` quedaba en None siempre. Medido en la copia:
    #     segment = jugador -> 100% con ancla (deposito 27/27, registro 33/33, retiro 3/3)
    #     segment = agente  ->   0% con ancla (deposito 0/90, retiro 0/23)
    # Arrastraba las TRES cosas que `ventana_juzgada` gobierna: la marca de que interaccion se
    # juzgo (el tablero no podia senalarla), los tiempos (describian la sesion entera en vez
    # del tramo) y sobre todo LA ATRIBUCION -- el bloque de mas abajo que reasigna la nota al
    # dueño de la ventana no se ejecutaba. **2 de 113 filas de agente quedaron mal
    # atribuidas**, las dos de 5 estrellas: alguien cobrando el trabajo de otro. En las otras
    # 111 acertaba por CASUALIDAD, porque el dominante de la sesion solia ser el mismo.
    if score is not None and ventana_juzgada is None:
        acotar = _ANCLA_POR_MOTIVO.get(score.motivo)
        ventana_juzgada = acotar(msgs) if acotar else None
    # LOS TIEMPOS Y EL OPERADOR DESCRIBEN LA INTERACCION QUE SE JUZGO, no el envase.
    # Los campos del CRM son del ENVASE: MEDIDO el 2026-08-12 sobre `f9b31f4f` (17
    # interacciones), `created_at` sale de la primera, `first_sent_message_at` de la segunda
    # (51,5 h despues) y `resolved_at` de la ultima -- cuatro interacciones en una fila. A
    # nivel poblacion: 1.208 sesiones de `jugador` (10,2%) con varias interacciones, con la
    # resolucion mostrada saltando de 3,4 h a 88,5-271,3 h (p90 de 3.834x contra la ventana
    # real). Y en 152 de 585 sesiones multi-interaccion de deposito/retiro (26,0%) la nota se
    # le cargaba a un operador que ni aparece en la interaccion juzgada.
    # LOS TIEMPOS SALEN DEL TRANSCRIPT, NUNCA DEL ENVASE. Si hay ancla determinista
    # (deposito/retiro), de la interaccion juzgada; si NO hay -- el fall-through al LLM, que
    # lee la sesion completa --, de la SESION entera. Medido en la corrida v6: el ventaneo
    # tapaba el 100% del camino determinista (91 de 91) y el 0% del fall-through (10 de 10),
    # donde los numeros volvian a los campos del CRM.
    # NO se acota la ventana del fall-through a proposito: ahi el LLM juzgo la sesion
    # completa, y elegir una interaccion seria decidir por el negocio cual representa la
    # nota de la sesion -- que sigue abierto.
    # Si NO hubo score (skipped) se dejan los campos del CRM: no hay nada juzgado que
    # describir, y no se inventa un tiempo para una fila sin nota.
    sess_medido, stats_medido = sess, stats
    if score is not None:
        ventana = ventana_juzgada or msgs
        inicio, primera_op, cierre = tiempos_de(ventana)
        if inicio is not None:
            sess_medido = {**sess, "created_at": inicio,
                           "first_sent_message_at": primera_op, "resolved_at": cierre}
            stats_medido = message_stats(ventana)
        # El operador se re-atribuye SOLO cuando hay ancla: acotar a la interaccion es lo que
        # da derecho a cambiarlo. Y solo si esa interaccion tiene uno identificable -- dejar
        # 'Operador sin identificar' seria cambiar una atribucion equivocada por ninguna.
        if ventana_juzgada:
            # El nombre de la nota se toma de la VENTANA: una conversacion reabierta tiene
            # varios cierres y no son la misma persona.
            op_name = nombre_de_notas(ventana_juzgada) or op_name
            op_id_ventana = primary_operator(ventana_juzgada)
            if op_id_ventana is not None:
                operator_id = op_id_ventana
                op_name = (op_map.get(str(op_id_ventana))
                           or operator_name(ventana_juzgada, op_id_ventana)
                           or nombre_de_notas(ventana_juzgada) or op_name)
    # rubric queda como el legacy human/bot (satisface chk_rubric); el motivo del LLM
    # se persiste en su propia columna dentro de build_score_record (desde el score).
    record = build_score_record(
        conversation=sess_medido, stats=stats_medido, rubric=rubric,
        eval_status=eval_status, skip_reason=skip_reason, score=score,
        operator_id=operator_id, operator_name=op_name, deposit_count=deposit_count,
        deposit_gate=deposit_gate, session_id=sess["session_id"],
    )
    # EL HECHO DEL ABANDONO VA EN TODAS LAS FILAS, no solo en las del camino LLM.
    # `score_by_motivo` lo mete en sus `dimensions`, pero las rubricas deterministas
    # (deposito/retiro/registro/promo/soporte/info/agilidad) devuelven su propio
    # ScoreResult sin pasar por ahi — el 63,2% de las sesiones. Sin esto el chip del front
    # aparecia en un tercio de los casos y el que mira no tenia forma de saber por que.
    # DONDE ARRANCA LA INTERACCION QUE SE JUZGO. Se guarda porque desde la fila NO se puede
    # deducir: si el ancla elige la PRIMERA interaccion, `conversation_created_at` queda
    # exactamente igual que si no hubiera ancla, y los dos casos piden marcados OPUESTOS en
    # el chat (senalar una, o no senalar ninguna). MEDIDO el 2026-08-12 sobre la copia: de 31
    # sesiones multi-interaccion muestreadas, 28 caian en esa ambiguedad. Sin el dato, el
    # modal senalaba la interaccion 1 como "la calificada" en sesiones donde el LLM habia
    # leido las 41. Ausente = no hay UNA interaccion que senalar (el fall-through leyo todo).
    if isinstance(record.get("dimensions"), dict) and ventana_juzgada:
        arranca = min(m["created_at"] for m in ventana_juzgada)
        record["dimensions"]["interaccion_juzgada_desde"] = arranca.isoformat()
    if isinstance(record.get("dimensions"), dict):
        record["dimensions"].setdefault(
            "cliente_abandono", cliente_abandono_tras_pedido(msgs))
        # QUE PASO CON EL CLIENTE (se_fue | no_lo_abrio | no_le_llego | dijo_no | None). El
        # booleano de arriba responde "¿le perdonamos al operador?" y exige que el cliente
        # haya LEIDO el pedido; este responde "¿que paso con el cliente?", que es lo que
        # necesita el negocio. MEDIDO: 48 clientes recibieron el pedido y nunca lo abrieron,
        # y esos desenlaces no se veian en ninguna parte. Ver signals.desenlace_del_cliente.
        record["dimensions"].setdefault("cliente_desenlace", desenlace_del_cliente(msgs))
        # CUANTA GENTE TRABAJO ESTA SESION. La fila es UNA nota con UN operador, pero una
        # sesion que el CRM reabre puede tener varias visitas con gente distinta: la nota se
        # la lleva el de mas mensajes y el trabajo del resto desaparece del denominador.
        # MEDIDO el 2026-08-14: 504 de 15.562 sesiones evaluadas (3,2%) tienen VARIOS
        # operadores, y ahi **1.824 de 2.734 interacciones (66,7%) son de alguien que NO
        # recibio la nota**. Llegan a 10 operadores en una sola fila.
        # NO SE MUEVE LA VENTANA: cualquiera que se elija deja ese 66,7% afuera del que cobra.
        # Partir es la solucion de raiz y el negocio la rechazo con numeros (docs/handoff.md
        # §10). Se MARCA, igual que `interaccion_juzgada_desde`: la fila declara su limite en
        # vez de mentir en silencio. Sacarlas del promedio moveria como maximo +0,045
        # estrellas, asi que el agregado NO es el problema -- lo es la fila que abre el
        # supervisor y lee "Anggie Belén, 4 estrellas" sobre una charla de seis personas.
        interacciones, operadores = reparto_por_interaccion(msgs)
        record["dimensions"].setdefault("interacciones_en_la_sesion", interacciones)
        record["dimensions"].setdefault("operadores_en_la_sesion", operadores)
    with conn.cursor() as cur:
        upsert_score(cur, record)
    conn.commit()
    return eval_status, skip_reason, score


def score_sessions_batch(conn, llm, account: str, limit: int, op_map: dict | None = None,
                         recommender=None, lineas: dict | None = None) -> dict:
    """Scorea un lote de sesiones pendientes de una cuenta. Devuelve conteos.

    `lineas`: mapa de las lineas propias (ver src/redireccion.build_lineas_map), para el
    skip por traspaso. Se construye aca si no viene, igual que `op_map`."""
    if op_map is None:
        with conn.cursor() as cur:
            op_map = build_operator_map(cur)
    if lineas is None:
        with conn.cursor() as cur:
            lineas = build_lineas_map(cur)
    with conn.cursor() as cur:
        pending = fetch_pending_sessions(cur, account, limit)
    counts = {"evaluated": 0, "skipped": 0, "error": 0, "seen": len(pending)}
    for sess in pending:
        try:
            eval_status, _, _ = score_session_and_store(conn, sess, llm, op_map,
                                                        recommender, lineas=lineas)
            counts[eval_status] += 1
        except Exception as e:  # noqa: BLE001 - no abortar el lote por una sesion
            # rollback: si el fallo fue DB-side, la txn queda abortada y cascadearia
            # al resto del lote (InFailedSqlTransaction) sin este reset.
            conn.rollback()
            counts["error"] += 1
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [worker] error sesion "
                  f"{sess.get('session_id') or sess.get('id')} ({account}): "
                  f"{type(e).__name__}: {str(e)[:300]}", flush=True)
            print(traceback.format_exc()[-1500:], flush=True)
    return counts


# Cada cuánto refrescar la tabla de conversión (determinista, sin LLM). No es por
# ciclo: es un recompute full-scale, alcanza cada tanto (el histórico cambia lento).
_CONV_REFRESH_SECONDS = 1800

# Clave del advisory lock de Postgres que serializa el worker de scoring a UNA sola
# instancia (evita deadlocks en conversation_sessions cuando hay varias réplicas).
_SCORING_LOCK_KEY = 823147


def _emit_stdout(msg: str) -> None:
    """El log del worker, CON FLUSH. No es cosmetico.

    EL SINTOMA (2026-08-24): el negocio dejo el scoring corriendo un fin de semana entero,
    volvio con ~500 filas y en los logs del contenedor no habia UNA SOLA linea `[worker]` --
    solo el access log de uvicorn. Sin esas lineas no se puede distinguir un worker que
    trabaja de uno que falla en cada sesion de uno que se detuvo, y las tres cosas se ven
    igual desde afuera: el worker es un THREAD DAEMON del mismo proceso que la API
    (src/app.py), asi que la web sigue contestando 200 aunque el thread ya no avance.

    LA CAUSA: `print` pelado. El stdout de un contenedor no es un TTY, asi que Python lo deja
    BLOCK-BUFFERED y las lineas se quedan en un buffer de 4-8 KB. uvicorn no sufre esto porque
    escribe por `logging`. El peor caso era el pre-flight `check_model()`: el aviso de que el
    modelo configurado no existe en Ollama -- la causa mas probable de que el loop no escriba
    ninguna fila -- era justo el mas invisible.
    """
    print(msg, flush=True)


def run_worker_loop(cfg, should_stop=None, log=_emit_stdout) -> None:
    """Loop continuo del contenedor: scorea pendientes por cuenta, duerme, repite."""
    import psycopg

    from src.conversions import refresh_account_conversions
    from src.sessions import refresh_account_sessions

    def emit(msg):
        """Log con timestamp (para leer la hora y el ritmo del goteo en prod)."""
        log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token, timeout=180.0,
                       num_ctx=cfg.ollama_num_ctx, num_predict=cfg.ollama_num_predict,
                       fast_attempts=cfg.llm_fast_attempts)
    # Sub-evaluadores angostos opcionales (2da pasada del LLM), gateados por config.
    recommender = None
    if cfg.recom_subagent_enabled:
        from src.subeval import build_recomendacion
        if cfg.recom_subagent_enabled:
            recommender = lambda m, mo, l: build_recomendacion(m, mo, l, llm)  # noqa: E731
    emit(f"[worker] iniciado · cuentas={cfg.scoring_accounts} batch={cfg.scoring_batch_size}"
         f" · recom_subagente={cfg.recom_subagent_enabled}")
    ok, msg = llm.check_model()  # pre-flight: no aborta, pero avisa fuerte si falta el modelo
    emit(f"[worker] {'preflight ok' if ok else 'PREFLIGHT FALLIDO'}: {msg}")
    # SINGLETON: solo UN worker scorea a la vez. Sin esto, varias réplicas del contenedor
    # (o el solape del restart-loop del health-check) arrancan cada una su propio loop y se
    # DEADLOCKEAN escribiendo conversation_sessions. Un advisory lock de sesión de Postgres
    # (atado a lock_conn; se libera solo si el proceso muere o cierra la conexión) garantiza
    # una sola instancia activa. Si no se obtiene, esta instancia NO scorea (se retira).
    # EL LOCK SE REINTENTA; NO SE ABANDONA. Hasta el 2026-08-24 esto se intentaba UNA vez y,
    # si otro lo tenía, la función hacía `return` y el thread moría. Eso costó un fin de
    # semana entero de scoring (logs de producción del 2026-08-21):
    #     19:26:04 [worker] lock de scoring adquirido (instancia única)
    #     ... cuatro ciclos sanos, err=0 en todos ...
    #     21:58:45 Started server process [1]      <- el contenedor reinició
    #     21:58:45 [worker] otra instancia ya tiene el lock; esta instancia NO scorea
    # El advisory lock está atado a la SESIÓN de Postgres de la instancia vieja: si el proceso
    # muere de golpe, el servidor puede tardar en reapear esa conexión (keepalives de TCP), así
    # que el reinicio cae justo en la ventana donde el lock todavía figura tomado. La web
    # siguió contestando 200 tres días y no se escribió una sola fila.
    # El guard singleton NO se debilita: sigue siendo `pg_try_advisory_lock` y sigue sin
    # scorear mientras otro lo tenga. Lo que cambia es que vuelve a intentarlo cada ciclo, así
    # que en cuanto la sesión zombi se cae, el scoring se reanuda solo.
    def _intentar_lock():
        try:
            conn = psycopg.connect(cfg.database_url, connect_timeout=8)
            conn.autocommit = True
            if conn.execute("SELECT pg_try_advisory_lock(%s)",
                            (_SCORING_LOCK_KEY,)).fetchone()[0]:
                return conn
            conn.close()
        except Exception as e:  # noqa: BLE001 - un fallo de red no puede matar el worker
            emit(f"[worker] no se pudo tomar el lock de scoring: {type(e).__name__}: {e}")
        return None

    def _dormir(segundos):
        """Sueño interrumpible: en tramos de 1s para que should_stop corte enseguida."""
        for _ in range(max(1, segundos)):
            if should_stop and should_stop():
                return
            time.sleep(1)

    lock_conn = None
    arrancado = False
    while not (should_stop and should_stop()):
        lock_conn = _intentar_lock()
        if lock_conn is not None:
            break
        emit("[worker] otra instancia tiene el lock de scoring; reintento en "
             f"{cfg.scoring_poll_seconds}s")
        _dormir(cfg.scoring_poll_seconds)
    if lock_conn is None:  # se pidió parar mientras esperaba el lock
        return
    emit("[worker] lock de scoring adquirido (instancia única)")
    # Migración AUTOMÁTICA a scoring por SESIÓN (una vez, antes de tocar columnas):
    # renombra la tabla vieja a backup y crea la fresca de grano sesión. Idempotente.
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                r = ensure_session_scoring_migration(cur)
            conn.commit()
        emit(f"[worker] migración: {r}")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] migración error: {type(e).__name__}: {e}")
    # Self-healing de columnas del pase LLM unificado (una vez, aditivo). La tabla de
    # prod ya existe; el CREATE ... IF NOT EXISTS no agrega columnas.
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                ensure_scores_columns(cur)
            conn.commit()
        emit("[worker] ensure_scores_columns ok")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] ensure_scores_columns error: {type(e).__name__}: {e}")
    # (Se retiró la corrección Opción B: v2 califica adquisición por motivo; el backfill
    # re-scorea todo con el pase por motivo, así que no hay filas que "limpiar".)
    # Sesionización inicial (una vez, antes del loop): asegura que el PRIMER ciclo tenga
    # sesiones para scorear (score_sessions_batch lee conversation_sessions).
    try:
        with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
            for account in cfg.scoring_accounts:
                with conn.cursor() as cur:
                    s = refresh_account_sessions(cur, account)
                conn.commit()
                emit(f"[worker] sesiones iniciales {account}: {s} sesiones")
    except Exception as e:  # noqa: BLE001 - no aborta el arranque del loop
        emit(f"[worker] sesiones iniciales error: {type(e).__name__}: {e}")
    last_conv = 0.0  # 0 -> corre en el primer ciclo (al arrancar)
    while not (should_stop and should_stop()):
        seen = 0
        llm_before = dict(llm.calls)  # snapshot para el delta fast/fallback del ciclo
        try:
            with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    op_map = build_operator_map(cur)
                # Lineas propias (connections) para el skip por `redireccion`. Una vez por
                # ciclo, como op_map: son 9 filas y cambian cuando el negocio migra una linea.
                with conn.cursor() as cur:
                    lineas = build_lineas_map(cur)
                for account in cfg.scoring_accounts:
                    t0 = time.time()
                    c = score_sessions_batch(conn, llm, account, cfg.scoring_batch_size, op_map,
                                             recommender=recommender,
                                             lineas=lineas)
                    dt = time.time() - t0
                    seen += c["seen"]
                    if c["seen"]:
                        rate = (c["evaluated"] / dt * 60) if dt > 0 else 0.0
                        emit(f"[worker] {account}: eval={c['evaluated']} skip={c['skipped']} "
                             f"err={c['error']} · {dt:.0f}s ({rate:.1f} eval/min)")
                # Pase de conversión (determinista): cada ~30min, no cada ciclo.
                if time.time() - last_conv >= _CONV_REFRESH_SECONDS:
                    for account in cfg.scoring_accounts:
                        try:
                            with conn.cursor() as cur:
                                n = refresh_account_conversions(cur, account)
                            conn.commit()
                            emit(f"[worker] conversión {account}: {n} personas")
                        except Exception as e:  # noqa: BLE001
                            conn.rollback()
                            emit(f"[worker] conversión {account} error: {type(e).__name__}: {e}")
                        # Sesionización (determinista, grano sesión): aditivo, no toca el scoring.
                        try:
                            with conn.cursor() as cur:
                                s = refresh_account_sessions(cur, account)
                            conn.commit()
                            emit(f"[worker] sesiones {account}: {s} sesiones")
                        except Exception as e:  # noqa: BLE001
                            conn.rollback()
                            emit(f"[worker] sesiones {account} error: {type(e).__name__}: {e}")
                    last_conv = time.time()
        except Exception as e:  # noqa: BLE001 - un fallo de red/DB no debe matar el loop
            emit(f"[worker] error de ciclo: {type(e).__name__}: {e}")
        # Delta LLM del ciclo: cuanto se resolvio por camino rapido vs fallback lento
        # (fallback alto = el modelo no devuelve el JSON al primer intento -> mas costo).
        d_fast = llm.calls["fast"] - llm_before["fast"]
        d_fb = llm.calls["fallback"] - llm_before["fallback"]
        d_empty = llm.calls["empty"] - llm_before["empty"]
        if d_fast or d_fb or d_empty:
            emit(f"[worker] llm ciclo: fast={d_fast} fallback={d_fb} empty={d_empty}")
        if seen == 0:  # nada pendiente -> goteo en calma; heartbeat y dormir en tramos
            emit(f"[worker] sin pendientes · durmiendo {cfg.scoring_poll_seconds}s")
            for _ in range(max(1, cfg.scoring_poll_seconds)):
                if should_stop and should_stop():
                    break
                time.sleep(1)
    lock_conn.close()  # libera el advisory lock en la parada ordenada (should_stop)
    emit("[worker] detenido")
