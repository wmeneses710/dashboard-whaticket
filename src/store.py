"""Armado y persistencia de filas en conversation_scores (UPSERT idempotente).

`build_score_record` es logica pura (testeable sin DB): junta datos de la
conversacion + metricas + router + (si aplica) el resultado del LLM en el dict
de columnas. `upsert_score` lo escribe por conversation_id.

La tabla es derivada y separada de las del ETL: es seguro TRUNCARLA y
re-scorear. Ver db/scores_schema.sql.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from src.metrics import (
    MessageStats,
    first_response_seconds,
    resolution_seconds,
    was_unassigned,
)
from src.scorer import ScoreResult
from src.segments import segment_for_queue

# 2026.08-rubricas-v5 (2026-08-11). El bump es OBLIGATORIO cada vez que cambia como se
# calcula la nota: sin el, las filas viejas y las nuevas quedan indistinguibles y no hay
# forma de comparar ni de volver atras. Lo que cambio contra v4:
#   - el GATE del comprobante deja de ser ciego al comprobante sin texto del cliente
#     (2 puertas: vocabulario real + acuse del operador) -> 23% de las que caian al pase
#     con LLM pasan a la rubrica determinista;
#   - "en breve tendras tu saldo disponible" ya NO cuenta como acreditacion;
#   - el ABANDONO exige que el cliente haya LEIDO el pedido (`messages.ack`): el 42,1% de
#     los abandonos que se reportaban eran inventados;
#   - PIEZA 6: devolverle la pelota al cliente que ya pidio registrarse es 'deficiente';
#   - el coaching apunta a la RAMA que produjo la nota, no a la estrella;
#   - `_PASO_RE` de soporte ve el vocabulario real del operador (+34,7% del bucket de 2);
#   - un emoji suelto y el saludo del widget web ya no se leen como pedido;
#   - se RETIRO el cap de uplift de `promo` (la PIEZA 2) y todo su cableado.
# Medido sobre 1.020 sesiones con el modelo de produccion: el promedio va de 4,03 a 3,97,
# pero el 93,8% de las notas NO se mueve — el movimiento se concentra en `deposito` (-0,35),
# `registro` (-0,14) y `soporte_cuenta` (+0,14).
#
# 2026.08-rubricas-v6 (2026-08-12). Sale de la auditoria de 5 frentes sobre los datos del
# rescore v5. Lo que cambio contra v5:
#   - CERRAR-Y-ADJUNTAR ES UN SOLO GESTO: la interaccion absorbe los mensajes del operador
#     que llegan hasta 2 min despues de la nota `*resuelto*`. El flujo real del retiro es
#     cerrar y mandar el comprobante con una MEDIANA DE 1,1 SEGUNDOS de diferencia, y ese
#     comprobante caia en la interaccion siguiente -> "nunca envio el comprobante".
#     VALIDADO re-corriendo la rubrica real: 132 de 139 retiros en 2 estrellas SUBEN
#     (113 a 4, 19 a 3), y ninguna de las 136 imagenes recuperadas es un broadcast
#     (`campaign_id` nulo en todas);
#   - un `*resuelto*` que se `*reabierto*` en el acto, sin que nadie hablara en el medio, no
#     es una frontera: es el CRM rebotando (7.406 pares, mediana 58,5 s);
#   - la PROMESA del operador ya no se lee como un PEDIDO: "te enviaremos el comprobante"
#     matcheaba el patron de abandono, y era el 99,0% de los abandonos de `retiro` y el
#     90,7% de los de agilidad en 5 estrellas;
#   - "ya puedes disfrutar tu saldo" ACREDITA: eran 106 sesiones en 2 estrellas y estaban
#     concentradas en una sola operadora (41,2% de sus notas contra el 10-11% de sus pares);
#   - los TIEMPOS y el OPERADOR describen la interaccion JUZGADA y no la conversacion, en
#     `deposito` y `retiro`: la resolucion mostrada baja de 118,5 h a 6,2 min de mediana en
#     324 sesiones, y se corrigen 150 de las 152 notas que se le cargaban a un operador que
#     ni aparecia en la interaccion juzgada;
#   - `deposit_mismatch` reconcilia contra LAS DOS PUERTAS del gate: 840 de 889 filas que
#     marcaban discrepancia no tenian ninguna (quedan las 49 del camino con LLM, que es
#     donde el flag significa algo).
#
# 2026.08-rubricas-v7 (2026-08-12). Sale de auditar la corrida v6 y el PROMPT contra el
# modelo real. Lo que cambio contra v6:
#   - `registro` entra al VENTANEO POR INTERACCION, del que se habia quedado afuera: sobre la
#     sesion entera emparejaba los datos de un alta con las credenciales de OTRA, y el
#     `convirtio` que habilita el 5 agarraba una recarga de cualquier interaccion mientras el
#     texto afirmaba "en la misma conversacion". VALIDADO re-corriendo la rubrica real sobre
#     1.717 sesiones: 1.508 de UNA interaccion no se mueven y **27 cambian, TODAS hacia
#     abajo** (13 de 5->4, 6 de 5->3, 4 de 5->2, 4 de 3->2);
#   - el texto ya no dice "Creo la cuenta NUNCA despues de recibir los datos": cuando las
#     credenciales salen ANTES de los datos la espera NO se puede medir, y la frase no la
#     afirma. Eran 14 filas, LAS 14 con 5 estrellas, mas 43 con "tardo nunca";
#   - PROMPT, `cliente_reinsistio`: reconocia el "?" literal y nada mas -- el caso mas
#     explicito ("llevo 40 minutos esperando", "me estan ignorando?") daba false. Es el hecho
#     que DEMOTA, asi que roto empujaba las notas hacia ARRIBA. Medido contra qwen3:14b: de
#     1 de 4 formas reconocidas a 5 de 5;
#   - PROMPT, `atendio_el_motivo`: una DESPEDIDA ya no atiende. "Mucha suerte hoy" es cierre,
#     no atencion; "listo"/"ing"/"cargado" siguen contando porque ACUSAN el pedido. Cerro la
#     inestabilidad que hacia alternar el mismo ghosteo entre `buena` y `deficiente`;
#   - PROMPT, `deposito` vs `problema`: un reclamo por una recarga YA HECHA (en pasado, sin
#     adjuntar nada) es `problema`, no `deposito`;
#   - PROMPT: se reescribio como HECHO la unica regla que quedaba redactada en terminos de la
#     NOTA ("la nota es aceptable, NO deficiente"), instruccion muerta desde `label_from_facts`;
#   - `retiro` y `registro` reportan `deposit_observed=None` (= no observo) en vez de un
#     booleano: `deposit_mismatch` reconcilia el gate contra la observacion del LLM, y una
#     rubrica determinista no tiene opinion que reconciliar. Eran 28 de los 48 mismatches.
# Banco de casos del prompt (scripts/eval_prompt.py): 26/28, estable en 3 repeticiones.
# NO se cambio el transcript: se probo darle tiempos y fronteras al modelo y NO mejora
# (26/28 sin tiempos contra 25/28 con), asi que `format_transcript(con_tiempos=)` queda
# apagado. Ver el docstring de esa funcion.
#
# 2026.08-rubricas-v8 (2026-08-12). Sale de auditar los 1★ y 2★ de una copia fresca con v7
# corriendo: de 7 leidos en detalle, 2 estaban bien puestos y 5 no. Lo que cambio contra v7:
#   - VOCABULARIO DE ACREDITACION, tercera ronda y la mas grande. El patron se escribio
#     leyendo PLANTILLAS y el texto libre del operador se le escapa: "Tu saldo ya está en tu
#     cuenta", "Su saldo ya se encuentra en su cuenta", "ya lo tienes en tu cuenta",
#     "ya te lo cargué" (¡"cargo" se reconocia y "cargué" no!), "ya está realizado".
#     VALIDADO: **154 de 323 (47,7%)** depositos en 2★ "nunca confirmo" SUBEN — 132 a 4★,
#     19 a 3★, 3 a 5★. Con las dos rondas previas ("en breve" el 11-08 y "ya puedes disfrutar
#     tu saldo" el 12-08) el vocabulario ya explica ~360 sesiones mal calificadas;
#   - RAMA DEL RECHAZO en `deposito`: cuando la plata NO podia entrar por una razon valida
#     (titular incorrecto, boleta repetida, cuenta sin verificar), el trabajo del operador es
#     AVISARLO, y se califica por la velocidad de ese aviso — 4 si avisa en <=2 min, 3 si
#     tarda, 2 si nunca dice nada. **TECHO EN 4 a proposito**: el 5 significa "el mejor
#     escenario del motivo" y un deposito rechazado no lo es; el techo es honesto y mantiene
#     el incentivo de ayudarlo a arreglarlo. Con su coaching propio, porque el del 4 normal
#     habla del bono y el del 3 del acuse, y ninguno aplica. Dispara en 5 sesiones: quirurgico.
# NO se toco el corte en interacciones: se audito sobre 576 interacciones reales y esta bien
# (0 de mas de 24 h, 0 visitas pegadas, las largas son el cliente demorando dentro de un mismo
# pedido o el operador esperando antes de cerrar). Ver el docstring de src/interacciones.py.
#
# 2026.08-rubricas-v9 (2026-08-12). NO cambia ninguna nota: agrega un dato para poder
# AUDITARLAS. `dimensions.interaccion_juzgada_desde` guarda donde arranca la interaccion que
# la rubrica miro, y se bumpea porque `dimensions` es parte de la nota persistida.
#   - POR QUE no se deduce de la fila: cuando el ancla elige la PRIMERA interaccion,
#     `conversation_created_at` queda IDENTICO a cuando no hay ancla, y los dos casos piden
#     marcados opuestos en el chat (senalar una, o no senalar ninguna). MEDIDO sobre la copia:
#     de 31 sesiones multi-interaccion muestreadas, 28 caian en esa ambiguedad.
#   - PARA QUE: el modal mostraba la sesion entera como un chat corrido, y una sesion mergea
#     todos los episodios del ticket -- hay de 41, 33 y 20 interacciones. Quien auditaba leia
#     una nota de 2★ al lado de un tramo que habia salido bien y concluia que el sistema se
#     equivocaba. La nota describe UNA interaccion, no la sesion. Ahora el chat lo dice.
#   - Las filas de v8 y anteriores no lo traen: ahi no se senala ninguna, que es lo honesto.
SCORING_VERSION = "2026.08-rubricas-v9"

# =============================================================================
# Forma CANÓNICA de conversation_scores (grano SESIÓN, todas las columnas
# actuales). store.py es la FUENTE de esta forma; db/scores_schema.sql debe
# mantenerse en sync con estas sentencias.
#
# Sin BEGIN/COMMIT ni ALTER de retrocompat: es la tabla FRESCA que crea la
# migración "desde cero con backup". No lleva `%` para no colisionar con el
# paramstyle de psycopg. Idempotente por CREATE ... IF NOT EXISTS.
# =============================================================================
_CREATE_SCORES_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_scores (
    conversation_id         uuid PRIMARY KEY,
    account                 text NOT NULL,
    ticket_id               uuid,
    segment                 text,
    queue_name              text,
    channel                 text,
    user_id                 uuid,
    user_name               text,
    conversation_created_at timestamptz,
    resolved_at             timestamptz,

    rubric                  text NOT NULL,
    eval_status             text NOT NULL,
    skip_reason             text,

    first_response_seconds  numeric,
    resolution_seconds      numeric,
    message_count           integer,
    agent_message_count     integer,
    bot_message_count       integer,
    contact_message_count   integer,
    was_unassigned          boolean,

    dimensions              jsonb,
    llm_model               text,

    rating_label            text,
    rating_rationale        text,

    resultado               text,
    deposit_count           integer,

    stars                   numeric,
    stars_breakdown         jsonb,

    is_estimate             boolean NOT NULL DEFAULT true,
    scoring_version         text,
    scored_at               timestamptz NOT NULL DEFAULT now(),

    atencion                text,
    deposit_observed        boolean,
    deposit_mismatch        boolean,
    session_id              uuid,
    -- Pase v2: motivo de la interaccion clasificado por el LLM (deposito, retiro,
    -- soporte_cuenta, info, promo, registro, problema). NULL en filas skipped o del
    -- pase viejo. Sin CHECK: la validez la garantiza el enum del schema del scorer.
    motivo                  text,

    -- rating_applicable: LEGACY de la Opción B (adquisición sin rating). v2 la retiró
    -- (promo/registro se califican por su motivo). Queda como true en toda fila
    -- scoreada; se conserva por compatibilidad con queries/dashboard.
    rating_applicable       boolean NOT NULL DEFAULT true,

    CONSTRAINT chk_rubric      CHECK (rubric IN ('human', 'bot')),
    CONSTRAINT chk_eval_status CHECK (eval_status IN ('evaluated', 'skipped')),
    CONSTRAINT chk_eval_coherence CHECK (
        (eval_status = 'skipped'   AND stars IS NULL     AND skip_reason IS NOT NULL) OR
        (eval_status = 'evaluated' AND skip_reason IS NULL)
    ),
    CONSTRAINT chk_stars_range CHECK (stars IS NULL OR (stars >= 1 AND stars <= 5))
)"""

# Índices de db/scores_schema.sql + idx por session_id (grano sesión).
_SCORES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_scores_account_segment ON conversation_scores (account, segment)",
    "CREATE INDEX IF NOT EXISTS idx_scores_user            ON conversation_scores (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_scores_created         ON conversation_scores (conversation_created_at)",
    "CREATE INDEX IF NOT EXISTS idx_scores_rubric_status   ON conversation_scores (rubric, eval_status)",
    "CREATE INDEX IF NOT EXISTS idx_scores_session         ON conversation_scores (session_id)",
)

# Nombre del backup de la tabla previa (grano conversación) que deja la migración.
_SCORES_BACKUP_TABLE = "conversation_scores_pre_session"


def _create_fresh_scores(cur) -> None:
    """Crea la tabla fresca conversation_scores + índices (idempotente)."""
    cur.execute(_CREATE_SCORES_TABLE)
    for stmt in _SCORES_INDEXES:
        cur.execute(stmt)


def ensure_session_scoring_migration(cur) -> dict:
    """Migración AUTOMÁTICA e IDEMPOTENTE "desde cero con backup".

    Al arrancar el servicio: renombra la tabla vieja conversation_scores a un
    backup (`conversation_scores_pre_session`) y crea una tabla FRESCA de grano
    sesión, para empezar el scoring de cero SIN perder lo anterior.

    Idempotente: el gate es la EXISTENCIA del backup.
      - Sin backup + tabla vieja presente -> RENAME + crea fresca. migrated=True.
      - Sin backup + sin tabla vieja (install nueva) -> solo crea fresca. migrated=False
        (no había nada que respaldar, no fue una migración real).
      - Con backup (ya migrado) -> NO re-renombra (no destruye); solo asegura la
        fresca (CREATE IF NOT EXISTS). migrated=False.

    Devuelve {"migrated": bool}; True SOLO cuando efectivamente renombró.
    """
    # Lock de transacción: dos workers arrancando a la vez (rolling deploy) podrían
    # competir en el RENAME. El advisory lock serializa la migración; se libera solo
    # al commit de la transacción del caller. El 2do worker espera y ve el backup ya
    # creado -> no re-renombra.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('conversation_scores_migration'))")
    cur.execute(f"SELECT to_regclass('{_SCORES_BACKUP_TABLE}')")
    backup = cur.fetchone()[0]
    if backup is None:
        cur.execute("SELECT to_regclass('conversation_scores')")
        old = cur.fetchone()[0]
        migrated = old is not None
        if migrated:
            cur.execute(
                f"ALTER TABLE conversation_scores RENAME TO {_SCORES_BACKUP_TABLE}"
            )
            # RENAME TABLE NO renombra los indices: quedan con sus nombres canonicos
            # pegados al backup, y el CREATE INDEX IF NOT EXISTS de la fresca los
            # saltearia (colision de nombre) dejandola SIN indices -> dashboard lento.
            # Liberamos los nombres canonicos renombrando los indices del backup.
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s",
                (_SCORES_BACKUP_TABLE,),
            )
            for (idxname,) in cur.fetchall():
                if not idxname.endswith("_presess"):
                    cur.execute(f'ALTER INDEX "{idxname}" RENAME TO "{idxname}_presess"')
        _create_fresh_scores(cur)
        return {"migrated": migrated}
    # Ya migrado: no tocar el backup ni la fresca existente, solo asegurar forma.
    _create_fresh_scores(cur)
    return {"migrated": False}

_COLUMNS = (
    "conversation_id", "account", "ticket_id", "segment", "queue_name", "channel",
    "user_id", "user_name", "conversation_created_at", "resolved_at",
    "rubric", "eval_status", "skip_reason",
    "first_response_seconds", "resolution_seconds",
    "message_count", "agent_message_count", "bot_message_count",
    "contact_message_count", "was_unassigned",
    "dimensions", "llm_model", "rating_label", "rating_rationale",
    "stars", "stars_breakdown", "deposit_count", "is_estimate", "scoring_version",
    "atencion", "deposit_observed", "deposit_mismatch", "session_id",
    "rating_applicable", "motivo",
)

# Columnas nuevas del pase LLM unificado. ensure_scores_columns() las agrega a una
# tabla de prod ya creada (el CREATE ... IF NOT EXISTS no agrega columnas). Mismo
# patron self-healing que conversions.ensure_table.
_SCORES_COLUMN_TYPES = (
    ("atencion", "text"),
    ("deposit_observed", "boolean"),
    ("deposit_mismatch", "boolean"),
    ("session_id", "uuid"),
    ("rating_applicable", "boolean NOT NULL DEFAULT true"),
    ("motivo", "text"),
)


def ensure_scores_columns(cur) -> None:
    """Agrega las columnas del pase LLM unificado si faltan (idempotente)."""
    for col, coltype in _SCORES_COLUMN_TYPES:
        cur.execute(
            f"ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS {col} {coltype}"
        )


def build_score_record(
    *,
    conversation: dict,
    stats: MessageStats,
    rubric: str,
    eval_status: str,
    skip_reason: str | None,
    score: ScoreResult | None,
    operator_id=None,
    operator_name: str | None = None,
    deposit_count: int = 0,
    deposit_gate: bool | None = None,
    session_id=None,
    scoring_version: str = SCORING_VERSION,
) -> dict[str, Any]:
    """Arma el dict de columnas para conversation_scores.

    `operator_id`/`operator_name` = operador reconstruido desde los mensajes (el
    conversations.user_id suele venir NULL). was_unassigned refleja el flag de
    asignacion de whaticket (conversations.user_id).
    """
    c = conversation
    segment = segment_for_queue(c.get("queue_name"))
    record: dict[str, Any] = {
        "conversation_id": c["id"],
        "account": c.get("account"),
        "ticket_id": c.get("ticket_id"),
        "segment": segment,
        "queue_name": c.get("queue_name"),
        "channel": c.get("channel"),
        "user_id": operator_id,
        "user_name": operator_name,
        "conversation_created_at": c.get("created_at"),
        "resolved_at": c.get("resolved_at"),
        "rubric": rubric,
        "eval_status": eval_status,
        "skip_reason": skip_reason,
        "first_response_seconds": first_response_seconds(
            c["created_at"], c.get("first_sent_message_at")
        ),
        "resolution_seconds": resolution_seconds(c["created_at"], c.get("resolved_at")),
        "message_count": stats.message_count,
        # La COLUMNA conserva el nombre legacy `agent_message_count`; el atributo de
        # MessageStats ya es `operator_message_count` (ver src/metrics.py). Renombrar la
        # columna exigiria migrar conversation_scores sin ganancia visible: nadie la ve.
        "agent_message_count": stats.operator_message_count,
        "bot_message_count": stats.bot_message_count,
        "contact_message_count": stats.contact_message_count,
        "was_unassigned": was_unassigned(c.get("user_id")),
        "dimensions": None,
        "llm_model": None,
        "rating_label": None,
        "rating_rationale": None,
        "stars": None,
        "stars_breakdown": None,
        "deposit_count": deposit_count,
        "is_estimate": True,
        "scoring_version": scoring_version,
        # Pase LLM unificado. En el path por-conversacion session_id llega None (lo
        # llena el paso 2). atencion/deposit_observed solo si hubo score.
        "atencion": None,
        "deposit_observed": None,
        "deposit_mismatch": _deposit_mismatch(deposit_count, score, deposit_gate),
        "session_id": session_id,
        # Motivo v2: lo llena el score (score_by_motivo). None en skipped / pase viejo.
        "motivo": None,
        # v2: el rating (por MOTIVO) aplica SIEMPRE que haya evaluación. Se retiró la
        # supresión Opción B en adquisición: promo/registro tienen su propia rúbrica y
        # SÍ se califican. Columna conservada (siempre true en filas scoreadas) por
        # compatibilidad con queries/dashboard.
        "rating_applicable": True,
    }
    if score is not None:
        record.update(
            llm_model=score.llm_model,
            atencion=score.atencion,
            deposit_observed=score.deposit_observed,
            motivo=score.motivo,
            dimensions={**score.dimensions, "recomendacion": score.recomendacion},
            rating_label=score.rating_label,
            rating_rationale=score.rating_rationale,
            stars=score.stars,
            stars_breakdown={
                "rubric": score.rubric,
                "label": score.rating_label,
                "stars": score.stars,
                "scoring_version": scoring_version,
                "floored": score.floor_applied,
            },
        )
    return record


def _deposit_mismatch(deposit_count: int, score: ScoreResult | None,
                      deposit_gate: bool | None = None) -> bool | None:
    """Reconciliacion determinista vs observacion del deposito (senal de calidad de dato).

    None si no se puede reconciliar (sin score o sin observacion del deposito).
    Si no: True cuando el gate determinista y la observacion discrepan. El determinista
    manda; el flag solo marca la discrepancia.

    `deposit_gate` = la respuesta de LAS DOS PUERTAS de `deposito.es_transaccion`. HAY QUE
    COMPARAR LA MISMA PUERTA: `deposit_count` sale de `deposit_candidate_count`, que exige
    que el CLIENTE escriba una palabra de recarga -- justo lo que la puerta 2 (el operador
    acusa el comprobante) existe para NO exigir. Sin esto, todo deposito que entra por la
    puerta 2 tiene `deposit_count=0` por construccion y el flag disparaba SIEMPRE.
    MEDIDO el 2026-08-12: 889 de 2.200 filas de `deposito` (40,4%), el 100% con
    `deposit_count=0`. La nota estaba bien; el indicador del dashboard mentia.
    None -> se degrada al criterio viejo, para no cambiar el path por conversacion.
    """
    if score is None or score.deposit_observed is None:
        return None
    determinista = (deposit_count > 0) if deposit_gate is None else deposit_gate
    return determinista != score.deposit_observed


# Columnas JSONB que hay que envolver para psycopg.
_JSONB_COLS = {"dimensions", "stars_breakdown"}


def upsert_score(cur, record: dict) -> None:
    """Inserta o actualiza la fila por conversation_id (idempotente)."""
    cols = list(_COLUMNS)
    placeholders = ", ".join(f"%({col})s" for col in cols)
    updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in cols if col != "conversation_id")
    sql = (
        f"INSERT INTO conversation_scores ({', '.join(cols)}, scored_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT (conversation_id) DO UPDATE SET {updates}, scored_at = now()"
    )
    params = {
        col: (Jsonb(record[col]) if col in _JSONB_COLS and record[col] is not None else record[col])
        for col in cols
    }
    cur.execute(sql, params)
