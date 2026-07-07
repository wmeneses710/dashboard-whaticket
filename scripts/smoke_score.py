"""Prueba de humo end-to-end: DB real -> prompt -> Ollama real -> resultado.

NO escribe en la BD (dry-run). Sirve para verificar la conexion con el modelo
y la calidad de la salida antes de construir el worker persistente.

Uso:
    python -m scripts.smoke_score            # 1 human + 1 bot de muestra
    python -m scripts.smoke_score --n 10     # 10 conversaciones recientes variadas
"""
from __future__ import annotations

import argparse
import time

import psycopg

from src.config import load_config
from src.context import fetch_messages, fetch_thread_context
from src.llm import OllamaClient
from src.scorer import score_conversation

PICK_SAMPLES = """
  (SELECT id FROM conversations
     WHERE user_id IS NOT NULL AND account='sistemas'
       AND resolved_at IS NOT NULL
     ORDER BY created_at DESC LIMIT %(half)s)
  UNION ALL
  (SELECT id FROM conversations
     WHERE user_id IS NULL AND account='sistemas'
       AND resolved_at IS NOT NULL
     ORDER BY created_at DESC LIMIT %(half)s)
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2, help="cantidad de conversaciones")
    args = ap.parse_args()

    cfg = load_config()
    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token, timeout=180.0)
    print(f"Ollama={cfg.ollama_url} modelo={cfg.ollama_model}\n")

    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn, conn.cursor() as cur:
        cur.execute(PICK_SAMPLES, {"half": max(1, args.n // 2)})
        ids = [r[0] for r in cur.fetchall()]

        for i, conv_id in enumerate(ids, 1):
            cur.execute(
                "SELECT ticket_id, user_id FROM conversations WHERE id=%s",
                (conv_id,),
            )
            ticket_id, user_id = cur.fetchone()
            rubric = "human" if user_id is not None else "bot"
            msgs = fetch_messages(cur, conv_id)
            ctx = fetch_thread_context(cur, ticket_id, conv_id)

            real = [m for m in msgs if not m["is_note"]]
            print(f"[{i}/{len(ids)}] conv={conv_id} rubric={rubric} "
                  f"agente_id={user_id or '-'} msgs_reales={len(real)}")

            t0 = time.perf_counter()
            try:
                r = score_conversation(
                    rubric=rubric, target_messages=msgs, thread_context=ctx, llm=llm
                )
            except Exception as e:  # noqa: BLE001 - es un smoke, queremos ver el fallo
                print(f"    ERROR: {type(e).__name__}: {e}\n")
                continue
            dt = time.perf_counter() - t0

            print(f"    -> {r.rating_label} ({r.stars} estrellas)  [{dt:.1f}s]")
            print(f"    porque: {r.rating_rationale}")
            errores = r.dimensions.get("errores") or []
            if errores:
                print(f"    errores: {errores}")
            print()


if __name__ == "__main__":
    main()
