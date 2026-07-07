"""PROTOTIPO — reconstruye el flujo REAL de un ticket (unidad = episodio de asignación).

Problema: el scorer trabaja sobre "conversaciones" (visitas) que el ETL fabrica
partiendo el ticket. Muchas son fragmentos o NO-interacciones (opener del anuncio +
asignado + resuelto sin respuesta), y el scorer las castiga como "deficiente". El LLM
no razona mal: le dan un pedazo suelto.

Este módulo arma la unidad real y la limpia:
  1. Ordena los mensajes cronológicamente.
  2. Segmenta en EPISODIOS DE ASIGNACIÓN: cada nota 'Asignado/aceptado' abre un
     episodio; los openers del cliente que la preceden se pegan a ESE episodio.
  3. Filtra las notas de sistema/internas (is_note) del transcript.
  4. Dedupea turnos idénticos consecutivos (el click-to-chat repite el opener 2-3x).
  5. Marca cada episodio scoreable según la SUSTANCIA DEL AGENTE (hubo atención real),
     no la longitud del cliente.

La función `reconstruct(messages)` opera sobre mensajes NORMALIZADOS (ver
`normalize_*`), así el inspector local (ndjson) y el test de scoring (BD) comparten
exactamente la misma lógica.

Uso local (lee los ndjson del ETL):
    python scripts/rebuild_flow.py                 # datos, 2026-06
    python scripts/rebuild_flow.py sistemas 2026-06 10
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

_AUDIT_DIR = Path(os.environ.get(
    "AUDIT_DIR", Path(__file__).resolve().parent.parent.parent / "ETLWhaticket" / "output"))

_ASSIGN_RE = re.compile(r"asignad|acept", re.I)     # abre un episodio de atención
_RESOLVE_RE = re.compile(r"resuelt|cerrad", re.I)    # lo cierra
_AGENT_SUBSTANCE_MIN = 20                            # chars mínimos para "atención real"


# --- Normalización: unifica las dos fuentes a un dict común ------------------
def normalize_ndjson(m: dict) -> dict:
    return {"from_me": bool(m.get("fromMe")), "is_note": bool(m.get("isNote")),
            "body": m.get("body"), "sent_from": m.get("sentFrom"),
            "user_id": m.get("userId"), "created_at": m.get("createdAt") or "", "_orig": m}


def normalize_db(m: dict) -> dict:
    return {"from_me": bool(m.get("from_me")), "is_note": bool(m.get("is_note")),
            "body": m.get("body"), "sent_from": m.get("sent_from"),
            "user_id": m.get("user_id"), "created_at": str(m.get("created_at") or ""), "_orig": m}


# --- Reconstrucción ----------------------------------------------------------
def _role(m: dict) -> str:
    if m["is_note"]:
        return "NOTE"
    if not m["from_me"]:
        return "CLIE"
    return "BOT" if m.get("sent_from") == "CHATBOT" else "AGEN"


def _text(m: dict) -> str:
    return (m["body"] or "[media]").replace("\n", " ").strip()


def _is_assign(m: dict) -> bool:
    return m["is_note"] and bool(_ASSIGN_RE.search(m["body"] or ""))


def segment_episodes(messages: list[dict]) -> list[list[dict]]:
    """Corta en episodios de asignación. Una nota 'Asignado/aceptado' abre un
    episodio nuevo; los mensajes del cliente que llegaron desde la última
    actividad del agente (el opener que disparó la asignación) se re-pegan al
    episodio nuevo, no al viejo."""
    msgs = sorted(messages, key=lambda m: m["created_at"])
    episodes: list[list[dict]] = []
    current: list[dict] = []
    for m in msgs:
        if _is_assign(m) and any(x["from_me"] and not x["is_note"] for x in current):
            # el episodio actual ya tuvo respuesta del agente -> cerrar. La cola
            # de mensajes solo-cliente (el nuevo opener) arranca el episodio siguiente.
            tail_start = len(current)
            while tail_start > 0 and not current[tail_start - 1]["from_me"]:
                tail_start -= 1
            head, tail = current[:tail_start], current[tail_start:]
            if head:
                episodes.append(head)
            current = tail
        current.append(m)
    if current:
        episodes.append(current)
    return episodes


def _dedupe(turns: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in turns:
        if out and out[-1]["role"] == t["role"] and out[-1]["text"] == t["text"]:
            continue
        out.append(t)
    return out


def rebuild_episode(ep_msgs: list[dict]) -> dict:
    real = [m for m in ep_msgs if not m["is_note"]]
    turns = _dedupe([{"role": _role(m), "text": _text(m)} for m in real])
    n_client = sum(1 for t in turns if t["role"] == "CLIE")
    n_agent = sum(1 for t in turns if t["role"] in ("AGEN", "BOT"))
    # Atención real = el negocio escribió algo con sustancia (no solo "Hola"/emoji).
    agent_substance = any(t["role"] in ("AGEN", "BOT") and len(t["text"]) >= _AGENT_SUBSTANCE_MIN
                          for t in turns)
    scoreable = bool(agent_substance and n_client > 0)
    skip_reason = None
    if not scoreable:
        skip_reason = "sin_atencion_real" if not agent_substance else "sin_cliente"
    # `raw` = los mensajes ORIGINALES (shape de la fuente) para pasarle al scorer.
    raw = [m.get("_orig", m) for m in real]
    return {"turns": turns, "raw": raw, "n_client": n_client, "n_agent": n_agent,
            "scoreable": scoreable, "skip_reason": skip_reason}


def reconstruct(messages: list[dict]) -> list[dict]:
    """messages ya NORMALIZADOS -> lista de episodios reconstruidos."""
    return [rebuild_episode(e) for e in segment_episodes(messages)]


# --- Inspector local (ndjson) ------------------------------------------------
def main() -> None:
    account = sys.argv[1] if len(sys.argv) > 1 else "datos"
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-06"
    n_show = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    files = sorted(glob.glob(str(_AUDIT_DIR / account / "messages_full" / month / "*.ndjson")))
    if not files:
        print(f"sin ndjson en {_AUDIT_DIR}/{account}/messages_full/{month}")
        return

    total_tickets = total_eps = scoreable_eps = 0
    shown = 0
    for f in files:
        for line in open(f):
            t = json.loads(line)
            eps = reconstruct([normalize_ndjson(m) for m in t.get("messages", [])])
            total_tickets += 1
            total_eps += len(eps)
            scoreable_eps += sum(1 for e in eps if e["scoreable"])
            if shown < n_show and len(eps) >= 2:
                shown += 1
                print(f"### TICKET {t.get('ticketId','')[:8]} · {t.get('contactName','?')} "
                      f"· agente={t.get('userName')} · visitas_ETL={t.get('conversationCount')} "
                      f"· episodios={len(eps)}")
                for i, e in enumerate(eps, 1):
                    mark = "SCOREABLE" if e["scoreable"] else f"SKIP ({e['skip_reason']})"
                    print(f"  ── ep {i} [{mark}] · {e['n_client']} cli / {e['n_agent']} ag")
                    for t2 in e["turns"]:
                        print(f"       {t2['role']}: {t2['text'][:72]}")
                print()

    skip_eps = total_eps - scoreable_eps
    print("=" * 60)
    print(f"cuenta={account} {month}: {total_tickets} tickets · {total_eps} episodios")
    print(f"  scoreables (atención real del agente): {scoreable_eps} "
          f"({100*scoreable_eps//max(1,total_eps)}%)")
    print(f"  a SALTAR (sin atención real): {skip_eps} "
          f"({100*skip_eps//max(1,total_eps)}%)  <- hoy el scorer los manda a 'deficiente'")


if __name__ == "__main__":
    main()
