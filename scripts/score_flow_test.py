"""PRUEBA A/B — scorea flujos RECONSTRUIDOS y compara contra el score per-visita viejo.

Hipótesis: el LLM no scorea mal; le damos fragmentos. Si le damos el episodio de
asignación COMPLETO (reconstruido, deduped, sin notas) en vez de la visita cruda del
ETL, el score debería ser más justo.

Qué hace:
  1. Toma N tickets que HOY tienen alguna visita 'deficiente' en conversation_scores.
  2. Reconstruye sus episodios desde los mensajes de la BD (misma lógica que
     scripts.rebuild_flow, unidad = episodio de asignación).
  3. Scorea cada episodio scoreable con el MISMO scorer/LLM de producción.
  4. Imprime, por ticket: score VIEJO (per-visita, de la tabla) vs NUEVO (per-episodio).

Corre en el contenedor (necesita DATABASE_URL + OLLAMA_* con token):
    python -m scripts.score_flow_test              # datos, 5 tickets
    python -m scripts.score_flow_test datos 8
"""
from __future__ import annotations

import sys

import psycopg

from src.config import load_config
from src.llm import OllamaClient
from src.metrics import message_stats
from src.router import decide_rubric
from src.scorer import score_conversation
from scripts.rebuild_flow import normalize_db, reconstruct

_TICKETS_SQL = """
SELECT ticket_id FROM conversation_scores
 WHERE account = %(account)s AND ticket_id IS NOT NULL
 GROUP BY ticket_id
HAVING count(*) FILTER (WHERE rating_label = %(label)s) >= 1
   AND count(*) > 1
 ORDER BY count(*) DESC
 LIMIT %(n)s
"""
_OLD_SQL = """
SELECT conversation_id, eval_status, rating_label, stars
  FROM conversation_scores WHERE ticket_id = %(tid)s
 ORDER BY conversation_created_at
"""
_MSGS_SQL = """
SELECT m.from_me, m.is_note, m.body, m.sent_from, m.user_id, m.media_type, m.created_at
  FROM messages m
  JOIN conversations c ON c.id = m.conversation_id
 WHERE c.ticket_id = %(tid)s
 ORDER BY m.created_at
"""


def _dedupe_raw(msgs: list[dict]) -> list[dict]:
    """Colapsa mensajes idénticos consecutivos (openers del anuncio repetidos)."""
    out: list[dict] = []
    for m in msgs:
        if out and out[-1].get("from_me") == m.get("from_me") and out[-1].get("body") == m.get("body"):
            continue
        out.append(m)
    return out


def _first_client(ep: dict) -> str:
    for t in ep["turns"]:
        if t["role"] == "CLIE":
            return t["text"][:80]
    return "(sin cliente)"


def _avg_old(rows: list[dict]) -> float | None:
    st = [float(r["stars"]) for r in rows if r["eval_status"] == "evaluated" and r["stars"] is not None]
    return sum(st) / len(st) if st else None


def main() -> None:
    account = sys.argv[1] if len(sys.argv) > 1 else "datos"
    n_tickets = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    cfg = load_config()
    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token, timeout=180.0)
    ok, msg = llm.check_model()
    print(f"[preflight] {'ok' if ok else 'FALLO'}: {msg}\n")

    with psycopg.connect(cfg.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_TICKETS_SQL, {"account": account, "label": "deficiente", "n": n_tickets})
            ticket_ids = [r[0] for r in cur.fetchall()]

        for tid in ticket_ids:
            with conn.cursor() as cur:
                cur.execute(_OLD_SQL, {"tid": tid})
                old = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
                cur.execute(_MSGS_SQL, {"tid": tid})
                msgs = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]

            episodes = reconstruct([normalize_db(m) for m in msgs])
            scoreables = [e for e in episodes if e["scoreable"]]

            old_avg = _avg_old(old)
            old_labels = ", ".join(f"{r['stars']}★/{r['rating_label']}" if r["eval_status"] == "evaluated"
                                   else f"skip/{r.get('skip_reason','')}" for r in old)
            print(f"### TICKET {str(tid)[:8]}")
            print(f"  VIEJO (per-visita): {len(old)} visitas · prom={old_avg}  [{old_labels}]")
            print(f"  NUEVO (per-episodio): {len(episodes)} episodios "
                  f"({len(scoreables)} scoreables, {len(episodes)-len(scoreables)} skip)")

            # --- Variante 2: por EPISODIO (una nota por sesión de asignación) ---
            ep_stars = []
            for i, ep in enumerate(scoreables, 1):
                ctx = "\n".join(f"- otra visita, cliente: {_first_client(o)}"
                                for o in scoreables if o is not ep)
                stats = message_stats(ep["raw"])
                rubric = decide_rubric(agent_message_count=stats.agent_message_count,
                                       bot_message_count=stats.bot_message_count)
                try:
                    res = score_conversation(rubric=rubric, target_messages=ep["raw"],
                                             thread_context=ctx, llm=llm)
                    ep_stars.append(res.stars)
                    print(f"    ep{i} [{rubric}] -> {res.stars}★ {res.rating_label}"
                          f"  · cli:'{_first_client(ep)}'")
                except Exception as e:  # noqa: BLE001
                    print(f"    ep{i} ERROR: {type(e).__name__}: {e}")
            ep_avg = sum(ep_stars) / len(ep_stars) if ep_stars else None

            # --- Variante 3: HILO ENTERO (todo el ticket como UNA experiencia) ---
            whole = _dedupe_raw([m for ep in scoreables for m in ep["raw"]])
            whole_star = None
            if whole:
                st = message_stats(whole)
                rub = decide_rubric(agent_message_count=st.agent_message_count,
                                    bot_message_count=st.bot_message_count)
                try:
                    res = score_conversation(rubric=rub, target_messages=whole,
                                             thread_context="", llm=llm)
                    whole_star = res.stars
                    print(f"    HILO [{rub}] -> {res.stars}★ {res.rating_label}")
                    print(f"        {(res.rating_rationale or '')[:170]}")
                except Exception as e:  # noqa: BLE001
                    print(f"    HILO ERROR: {type(e).__name__}: {e}")

            print(f"  >> VIEJO(visita)={old_avg}  ·  EPISODIO={ep_avg}  ·  HILO={whole_star}\n")


if __name__ == "__main__":
    main()
