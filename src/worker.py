"""Worker de scoring: puntua conversaciones PENDIENTES de una cuenta.

Reutilizable por el batch manual (scripts/run_scoring.py) y por el loop en
background del contenedor (src/app.py). Idempotente: solo toma conversaciones
que todavia no estan en conversation_scores. Scopeado por cuenta: datos y
sistemas conviven en la misma BD y el worker procesa las cuentas configuradas.
"""
from __future__ import annotations

import time
import traceback
from datetime import timedelta

from src.agilidad import score_agilidad
from src.context import fetch_messages, fetch_session_messages, fetch_thread_context
from src import errores
from src.cola_de_cortesia import decidir_con_el_modelo, necesita_el_modelo
from src.deposito import es_transaccion as es_transaccion_deposito
from src.deposito import score_deposito
from src.deposito import interaccion_juzgada as interaccion_juzgada_deposito
from src.deposits import deposit_candidate_count
from src.interacciones import SILENCIO_MAX, partir_en_interacciones, tiempos_de
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
    ensure_interaccion_scoring_migration,
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


def _ultimo_del_negocio(msgs_sesion: list[dict], desde) -> str:
    """Lo ultimo que el negocio le dijo al cliente ANTES de este fragmento.

    Es el contexto sin el cual el modelo no puede decidir: "ya esta" contesta a "tu saldo ya
    esta disponible", y plantea algo si viene solo.
    """
    previos = [m for m in msgs_sesion
               if m.get("from_me") and not m.get("is_note")
               and m.get("created_at") is not None
               and (desde is None or m["created_at"] < desde)]
    return (previos[-1].get("body") or "") if previos else ""


def _disposicion_de_la_cola(msgs: list[dict], ultimo_del_negocio: str, llm):
    """(eval_status, skip_reason, score) si el fragmento es una cola de cortesia; None si no.

    LA COMPUERTA DE LA CAPA 2. `score_sin_respuesta` corre PRIMERO y esa prioridad es del
    negocio ("si no hubo respuesta, ese caso manda"): NO se invierte. Lo que se le pone
    adelante es esta compuerta, que solo se abre para el RESIDUO -- el fragmento que ya
    parece cola pero que la capa determinista no reconoce (24 en 30 dias) -- y solo si el
    modelo afirma que no habia nada que contestar.

    DEVUELVE None ANTE CUALQUIER DUDA, y ese es el punto: sin LLM, con el modelo caido, con
    una respuesta fuera del enum o con una excepcion, la nota es la de siempre. Una
    inferencia que no llego no puede borrar una falla real. Ver src/cola_de_cortesia.py.

    SKIP y no nota: `solo_cortesia` daria 3 estrellas aca (el cliente SIEMPRE se queda con la
    ultima palabra en este caso) y su `rubric="human"` declara que el negocio escribio, que
    es falso. El skip lleva causa propia y `SKIP_LABEL` la desglosa, asi que la fila se sigue
    contando en la tarjeta de sin evaluar -- que es lo que el negocio pidio cuando saco
    `redireccion` de ser un skip mudo el 2026-08-20.

    LA DISTANCIA NO DECIDE EL CASTIGO (decision del negocio, 2026-08-28). Textual: *"merece
    el skip porque para el usuario es la misma conversacion, no quiere nada mas, no se debe
    castigar a ATC por algo que no requiere atencion"*. Eso separa dos cosas que estaban
    pegadas: `GRACIA_CORTESIA_SEG` (10 min) gobierna la ATRIBUCION -- si la cola se pega, el
    operador anterior se lleva la evidencia de que el cliente quedo conforme -- y NO gobierna
    el castigo. Cerraba el hueco mas grande: de los 65 fragmentos que cobraban 1 estrella en
    30 dias, **35 eran cortesia pura fuera de la ventana**, y ni la capa 1 los pegaba ni la
    compuerta les preguntaba.
    """
    reales = [m for m in msgs if not m.get("is_note")]
    if not reales or any(m.get("from_me") for m in reales):
        # El negocio escribio (o no hay nada real): la atencion existio.
        return None
    # CAPA 1, y GRATIS: si el determinista ya sabe que es cortesia, no se paga inferencia.
    # `client_sin_motivo` esta verificada 40/40 contra el modelo (scripts/bench_sin_motivo.py),
    # asi que preguntarle seria pagar para que confirme.
    from src.signals import client_sin_motivo
    if client_sin_motivo(reales):
        return "skipped", "cola_de_cortesia", None
    # CAPA 2: el residuo -- lo que parece cola pero el patron no reconoce (typos, 'Liato').
    if llm is None or not necesita_el_modelo(msgs):
        return None
    try:
        es_cola = decidir_con_el_modelo(msgs, ultimo_del_negocio, llm)
    except Exception:  # noqa: BLE001 - un fallo del modelo no puede tumbar el scoring
        return None
    if es_cola is not True:
        return None
    return "skipped", "cola_de_cortesia", None


def _registrar_fallo(component: str, exc: BaseException, account: str | None,
                     context: dict) -> None:
    """Persiste el fallo en la tabla `errors` sin que eso pueda tumbar el lote.

    `account` va en la COLUMNA y no solo en el contexto: el indice del ETL es
    `(account, component, occurred_at DESC)` y el loop atiende varias cuentas (regla 6 del
    equipo del ETL, 2026-08-28).

    `errores.registrar` ya promete no levantar, pero el lote NO puede depender de esa
    promesa: si algun dia rompe, se lleva puesto el manejo del error original y con el las
    sesiones que faltaban. Es la misma cautela que la regla 1 de src/errores.py, del lado
    del llamador. Ver tests/test_worker.py::test_si_el_registrador_falla_el_lote_SIGUE.
    """
    try:
        errores.registrar(component, exc, account=account, context=context)
    except Exception:  # noqa: BLE001 - un registrador roto no puede abortar el lote
        pass


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
            cid = conv.get("conversation_id") or conv.get("id")
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [worker] error conv "
                  f"{cid} ({account}): "
                  f"{type(e).__name__}: {str(e)[:300]}", flush=True)
            print(traceback.format_exc()[-1500:], flush=True)
            # El `print` se queda: es lo que se ve en `docker logs` MIENTRAS pasa. La fila
            # es para despues, cuando el log ya se roto.
            _registrar_fallo("scoring", e, account,
                            {"conversation_id": str(cid) if cid else None,
                             "grano": "conversacion"})
    return counts


# --- Scoring por SESION -----------------------------------------------------------
# Espeja el path por-conversacion (PENDING_SQL/fetch_pending/score_and_store/
# score_batch) pero a grano SESION. CUANDO se scorea: cuando su ultima atencion CERRO, y
# eso ahora se decide distinto segun COMO cerro -- ver el WHERE de PENDING_SESSIONS_SQL.
# (Era un hold fijo de 6h para todas; el 2026-08-27 paso a ser condicional.) La
# fila resultante queda keyeada por conversation_id = session_id (la conversacion de
# ENTRADA, el primer episodio de la sesion) con la columna session_id seteada.
# run_worker_loop YA usa este path (el flip se hizo). El path por-conversacion
# (PENDING_SQL/fetch_pending/score_and_store/score_batch) queda como API para el batch
# manual (scripts/), pero el loop del contenedor scorea por sesion.
# Cuanto se espera despues del cierre antes de calificar. Ver el comentario del WHERE.
# El corto tiene que cubrir el p99 del ETL (36 s) con margen y NO volver a empujar la
# alerta fuera de horario; el largo es el mismo silencio con el que se corta la interaccion.
HOLD_CERRADA = timedelta(minutes=5)
HOLD_SIN_CIERRE = SILENCIO_MAX

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
   AND (
     -- EL HOLD ES CONDICIONAL (2026-08-27). Antes eran 6 HORAS FIJAS para todas, para
     -- reconciliar un posible REGRESO del cliente: si volvia, la sesion crecia y la nota
     -- quedaba vieja. Con el grano INTERACCION eso dejo de aplicar: si el cliente vuelve,
     -- eso es una atencion NUEVA con su propia nota, y la anterior ya no cambia.
     --
     -- Y el hold TAMPOCO hacia falta para el ETL, que era la otra sospecha. MEDIDO sobre
     -- 9.366 atenciones, desde la nota de cierre hasta que el ETL termino de capturarla:
     --     p50 = 18 s  ·  p90 = 30 s  ·  p99 = 36 s  ·  max = 64 min
     -- Ya esta todo en menos de un minuto. Los 9 min que conociamos son de conversaciones
     -- VIVAS (nadie toco el ticket); cerrar es justamente tocarlo.
     --
     -- LO QUE COSTABA: las 7 alertas VIP del 26-ago cerraron entre las 18:30 y las 21:06 y
     -- se avisaron entre las 00:38 y las 03:18. Ninguna la leyo nadie.
     --
     -- EL OPERADOR YA LA CERRO -> el fin es un HECHO DECLARADO, no una inferencia.
     (cs.end_at < now() - make_interval(secs => %(hold_cerrada)s)
      AND EXISTS (
        SELECT 1 FROM conversation_session_map map
          JOIN messages n ON n.conversation_id = map.conversation_id
         -- `account` PRIMERO y no por gusto: el indice es (account, session_id), y
         -- filtrando solo por session_id no se usa. MEDIDO con EXPLAIN ANALYZE sobre la
         -- copia: sin el, Seq Scan sobre conversation_session_map 120.843 veces
         -- descartando 74.897 filas cada una -> 162 SEGUNDOS para traer 5 filas.
         WHERE map.account = cs.account AND map.session_id = cs.session_id
           AND n.is_note AND n.body LIKE '%%*resuelto*%%'
           -- Cerca del fin de la sesion: una nota de cierre vieja es de OTRA atencion.
           -- El margen cubre `GRACIA_CIERRE_SEG` (el operador cierra y adjunta despues,
           -- asi que la nota puede quedar ANTES del ultimo mensaje real).
           AND n.created_at >= cs.end_at - interval '3 minutes'))
     -- NADIE LA CERRO -> lo unico que dice que termino es el silencio, y ese umbral es
     -- `SILENCIO_MAX` (src/interacciones.py), el MISMO con el que se corta la interaccion.
     -- Es 3 de cada 100 casos. (SIN el signo de porcentaje: psycopg parsea el SQL
     -- entero buscando placeholders y uno suelto revienta con 'incomplete
     -- placeholder'. Los tests de string pasaban con la consulta ROTA; se detecto
     -- ejecutandola contra la copia.)
     OR cs.end_at < now() - make_interval(secs => %(hold_sin_cierre)s)
   )
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
    cur.execute(PENDING_SESSIONS_SQL, {
        "account": account, "limit": limit,
        "hold_cerrada": HOLD_CERRADA.total_seconds(),
        "hold_sin_cierre": HOLD_SIN_CIERRE.total_seconds(),
    })
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def score_session_and_store(conn, sess: dict, llm, op_map: dict,
                            recommender=None, lineas: dict | None = None):
    """Scorea la sesion INTERACCION POR INTERACCION y persiste UNA FILA POR CADA UNA.

    EL CAMBIO DE GRANO (2026-08-27). `partir_en_interacciones` existe desde el 2026-08-11
    y ya lo usan deposito, retiro, registro, agilidad y el front, pero este worker lo usaba
    solo para ELEGIR cual calificar (el ancla-ultima) y descartaba el resto.

    MEDIDO sobre la copia: **2.145 conversaciones contienen 10.381 atenciones de operadores
    distintos** -- hasta 14 personas en una sola fila -- y se repartian 2.145 notas.
    **8.236 atenciones no recibian ninguna** y el trabajo de quien las hizo no entraba en
    ningun denominador.

    EL RETORNO NO CAMBIA: sigue siendo el de la ULTIMA interaccion, que es exactamente la
    que el ancla-ultima calificaba. Asi el contrato de `score_sessions_batch` se conserva y
    lo que se agrega son las filas que faltaban, no un cambio de forma.

    Y EL LLM PASA A VER UNA SOLA ATENCION. Ademas de ser lo correcto es mas barato: hasta
    hoy el modelo leia semanas de charla para calificar una atencion de tres minutos.
    """
    with conn.cursor() as cur:
        msgs = fetch_session_messages(cur, sess["session_id"])
    partes = partir_en_interacciones(msgs) or [msgs]
    resultado = (None, None, None)
    for seq, interaccion in enumerate(partes, 1):
        resultado = _score_interaccion_y_persiste(
            conn, sess, interaccion, msgs, seq, llm, op_map, recommender, lineas)
    return resultado


def _score_interaccion_y_persiste(conn, sess: dict, msgs: list[dict],
                                  msgs_sesion: list[dict], seq: int,
                                  llm, op_map: dict, recommender, lineas: dict | None):
    """Scorea UNA interaccion y persiste su fila. Devuelve (eval_status, skip_reason, score).

    Es el cuerpo que antes corria una vez por sesion: `ventana_juzgada`, los tiempos y la
    re-atribucion ya trabajaban sobre una ventana, asi que lo unico que cambio es QUE
    transcript recibe. El ancla sigue acotando ADENTRO de la interaccion (el comprobante
    dentro de la atencion), que es una pregunta distinta y sigue valiendo.
    """
    # `if msgs` y no a secas: `fetch_session_messages` puede devolver [] (una sesion sin
    # mensajes mapeados) y `min()` sobre vacio lanza ValueError. El lote lo atrapa y sigue,
    # pero la sesion se perderia EN SILENCIO y volveria como pendiente en cada pasada.
    # Sin ventana, `build_score_record` cae a la clave legacy (el conversation_id), asi que
    # la fila igual se persiste y la sesion deja de estar pendiente.
    interaccion_ini = min(m["created_at"] for m in msgs) if msgs else None
    interaccion_fin = max(m["created_at"] for m in msgs) if msgs else None
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
        # LA COMPUERTA DE LA CAPA 2, y va DESPUES de calcular `score_sin_respuesta` a
        # proposito: solo se consulta al modelo por un fragmento que EFECTIVAMENTE iba a
        # cobrar el 1 estrella. Si `sin_respuesta` no dispara, no hay nada que evitar y no se
        # paga inferencia. Ver `_disposicion_de_la_cola` y src/cola_de_cortesia.py.
        if score is not None:
            _cola = _disposicion_de_la_cola(
                msgs, _ultimo_del_negocio(msgs_sesion, interaccion_ini), llm)
            if _cola is not None:
                eval_status, skip_reason, score = _cola
        if score is not None:
            pass
        elif eval_status != "evaluated":
            # La compuerta lo saco de `evaluated`: no se le busca rubrica.
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
    # LOS TIEMPOS SALEN DE LA VENTANA SIEMPRE, TAMBIEN EN LAS `skipped`.
    # Hasta el grano interaccion las filas sin nota conservaban los campos del CRM, con el
    # argumento de que "no hay nada juzgado que describir" y no se inventa un tiempo para
    # una fila sin nota. Ese argumento se cayo: la ventana de la interaccion NO es una
    # inferencia, es un hecho medido -- sabemos cuando empezo y cuando cerro esa atencion,
    # la hayamos calificado o no.
    # VISTO EN EL RESCORE LOCAL del 2026-08-27: una fila `skipped` con ventana de 91
    # segundos declaraba `resolution_seconds = 253.699` (2,9 DIAS), y otra con `ini == fin`
    # declaraba 444 s. La fila no quedaba incompleta: MENTIA.
    sess_medido, stats_medido = sess, stats
    ventana = (ventana_juzgada if score is not None else None) or msgs
    inicio, primera_op, cierre = tiempos_de(ventana)
    if inicio is None:
        # PURAS NOTAS INTERNAS (`internal_notes_only`, message_count = 0). `tiempos_de`
        # mide sobre mensajes REALES y ahi no hay ninguno, asi que devuelve None -- y sin
        # esta rama la fila volvia a heredar el envase, que es justo lo que el ventaneo
        # viene a impedir. VISTO en el rescore del 2026-08-27: dos filas de 0 mensajes
        # declarando 32 s y 253 s de un envase de dias.
        # Se llega aca por el fallback `partir_en_interacciones(msgs) or [msgs]`: si la
        # sesion entera es notas, el corte devuelve vacio y se scorea el bloque completo.
        # La VENTANA si se conoce -- interaccion_ini/fin salen de TODOS los mensajes,
        # notas incluidas -- asi que se usa esa. No se inventa nada.
        inicio, cierre = interaccion_ini, interaccion_fin
    if inicio is not None:
        sess_medido = {**sess, "created_at": inicio,
                       "first_sent_message_at": primera_op, "resolved_at": cierre}
        stats_medido = message_stats(ventana)
    if score is not None:
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
        interaccion_seq=seq, interaccion_ini=interaccion_ini,
        interaccion_fin=interaccion_fin,
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
        # SOBRE LA SESION ENTERA, no sobre esta interaccion: el dato responde "¿de cuantas
        # atenciones forma parte esta?" y con el grano interaccion sirve para lo contrario
        # que antes. Antes marcaba un LIMITE de la fila (una nota sobre el trabajo de
        # varios); ahora es contexto — cada uno ya tiene su nota, y esto ubica la fila
        # dentro del hilo del cliente. Con `msgs` daria 1 siempre y no diria nada.
        interacciones, operadores = reparto_por_interaccion(msgs_sesion)
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
            sid = sess.get("session_id") or sess.get("id")
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [worker] error sesion "
                  f"{sid} ({account}): "
                  f"{type(e).__name__}: {str(e)[:300]}", flush=True)
            print(traceback.format_exc()[-1500:], flush=True)
            # El `print` se queda: es lo que se ve en `docker logs` MIENTRAS pasa. La fila es
            # para despues, cuando el log ya se roto -- el caso de src/llm.py:207, donde una
            # misma sesion fallo ~15 veces en tres horas y se diagnostico contra el log vivo.
            _registrar_fallo("scoring", e, account,
                            {"session_id": str(sid) if sid else None,
                             "grano": "interaccion"})
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

    import os

    from src.alertas import barrer as barrer_alertas
    from src.alertas import canal_desde_env
    from src.conversions import refresh_account_conversions
    from src.sessions import refresh_account_sessions

    def emit(msg):
        """Log con timestamp (para leer la hora y el ritmo del goteo en prod)."""
        log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

    # UN canal de Telegram para las dos alertas (ver src/alertas.py). Si le falta el token
    # o el chat, calla y el barrido lo saltea SIN error: se puede subir el worker con las
    # alertas puestas y el grupo todavia sin crear.
    canal_vip = canal_desde_env(os.environ)
    emit(f"[worker] alertas VIP: {'on' if canal_vip.configurado else 'off'}")
    # Bitacora de fallos que SOBREVIVE al redeploy (tabla `errors`, compartida con el ETL).
    # Se configura una sola vez, como el logging: despues `_registrar_fallo` escribe desde
    # donde haga falta. Sin esto es un no-op, que es lo que permite correr los tests y el
    # local sin BD. NO se pasa cuenta: el loop atiende VARIAS y cada fila lleva la suya en
    # el contexto (ver los dos handlers de lote).
    errores.configurar(cfg.database_url)
    # La linea que distingue "el deploy entro" de "el deploy no entro". Si dice que falta la
    # columna `source`, el dashboard subio ANTES que el ETL y no va a registrar nada.
    emit(f"[worker] bitacora de errores: {errores.estado()}")
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
                # Y despues al grano INTERACCION (2026-08-27). En ESTE orden: la de sesion
                # es la que libera los nombres de indice, y la de interaccion respalda lo
                # que aquella dejo. Las dos son idempotentes y tienen su propio backup, asi
                # que una base ya migrada pasa por las dos sin tocar nada.
                # SIN ESTO el deploy arranca, no falla, y sigue pisando una interaccion con
                # la siguiente en silencio: `CREATE TABLE IF NOT EXISTS` no cambia una PK.
                ri = ensure_interaccion_scoring_migration(cur)
            conn.commit()
        emit(f"[worker] migración: sesión={r} interacción={ri}")
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
                    # ALERTA DE JUGADOR VIP. Va DESPUES del lote y no antes: el resumen
                    # sale de la sesion recien calificada, asi que un aviso llega en el
                    # mismo ciclo en que la charla se cerro. `barrer` no lanza nunca y con
                    # el canal apagado no hace nada: el scoring es el producto.
                    a = barrer_alertas(conn, account, canal_vip, log=emit)
                    # SE LOGUEA TAMBIEN CUANDO FALLA. Sin el conteo de fallos, un canal
                    # caido devolvia los mismos ceros que un dia tranquilo y no escribia
                    # una sola linea.
                    if a["resumen"] or a["fallos"]:
                        emit(f"[worker] {account}: alertas VIP "
                             f"resumen={a['resumen']} fallos={a['fallos']}")
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
