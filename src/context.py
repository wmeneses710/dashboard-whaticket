"""Lectura de mensajes y armado del contexto del hilo del ticket.

El contexto = las OTRAS visitas del mismo ticket (una linea por visita), para
que el scorer no juzgue a ciegas un fragmento. Se capa a las ultimas
MAX_THREAD_VISITS visitas: sin tope, un ticket con cientos de visitas genera un
prompt gigante que confunde al modelo (lo vimos con libros contables internos).
"""
from __future__ import annotations

# Cuantas visitas previas del ticket se le muestran al modelo como contexto.
MAX_THREAD_VISITS = 8


def fetch_messages(cur, conversation_id) -> list[dict]:
    """Mensajes de la conversacion en orden cronologico (incluye notas; el
    transcript las excluye despues).

    Trae sent_from (para distinguir bot/humano) y user_id (para atribuir el
    operador, ya que conversations.user_id suele venir NULL)."""
    cur.execute(
        "SELECT from_me, is_note, body, sent_from, user_id, media_type FROM messages "
        "WHERE conversation_id=%s ORDER BY created_at",
        (conversation_id,),
    )
    return [
        {"from_me": r[0], "is_note": r[1], "body": r[2], "sent_from": r[3],
         "user_id": r[4], "media_type": r[5]}
        for r in cur.fetchall()
    ]


def format_thread_digest(visits: list[dict], max_visits: int = MAX_THREAD_VISITS) -> str:
    """Arma el digest (una linea por visita), capado a las mas recientes.

    `visits`: dicts con created_at, is_bot, first_customer_msg, en orden
    cronologico ascendente.
    """
    if not visits:
        return ""
    omitidas = len(visits) - max_visits
    recientes = visits[-max_visits:]
    lines = []
    if omitidas > 0:
        lines.append(f"(... {omitidas} visitas previas omitidas ...)")
    for v in recientes:
        quien = "BOT" if v["is_bot"] else "AGENTE"
        snippet = (v.get("first_customer_msg") or "(sin mensaje de cliente)")[:90]
        lines.append(f"- {v['created_at']:%Y-%m-%d %H:%M} [{quien}] cliente: {snippet}")
    return "\n".join(lines)


def fetch_thread_context(cur, ticket_id, target_id, max_visits: int = MAX_THREAD_VISITS) -> str:
    """Trae las otras visitas del ticket (capadas a las mas recientes) y las
    formatea como contexto."""
    if ticket_id is None:
        return ""
    cur.execute(
        """SELECT c.created_at, c.user_id IS NULL AS is_bot,
                  (SELECT body FROM messages m
                     WHERE m.conversation_id=c.id AND NOT m.is_note AND NOT m.from_me
                     ORDER BY m.created_at LIMIT 1) AS first_customer_msg
             FROM conversations c
            WHERE c.ticket_id=%s AND c.id<>%s
            ORDER BY c.created_at DESC
            LIMIT %s""",
        (ticket_id, target_id, max_visits),
    )
    rows = cur.fetchall()[::-1]  # a cronologico ascendente
    visits = [
        {"created_at": r[0], "is_bot": r[1], "first_customer_msg": r[2]} for r in rows
    ]
    return format_thread_digest(visits, max_visits)
