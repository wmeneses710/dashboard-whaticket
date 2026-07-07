"""Prueba el prompt/elegibilidad AFINADOS con los dos casos reales (Miguel, Eduardo).

Miguel: el cliente solo mando 2 imagenes (media) + el agente cerro cordial. El LLM
no puede leer las imagenes -> debe SALTARSE (customer_media_only), no inventar un 2.

Eduardo: el cliente pregunta por el bono, el agente ofrece crear la cuenta y manda el
formulario de registro (paso accionable), el cliente no sigue. Con las reglas nuevas
(paso accionable cuenta, no castigar por abandono del cliente, tono cordial) deberia
salir ~3 (aceptable), no 2 (deficiente).

Corre en el contenedor (necesita OLLAMA_* con token):
    python -m scripts.test_prompt_examples
"""
from __future__ import annotations

from src.config import load_config
from src.llm import OllamaClient
from src.metrics import message_stats
from src.router import decide_eligibility, decide_rubric
from src.scorer import score_conversation


def _c(body):   # cliente
    return {"from_me": False, "is_note": False, "body": body, "sent_from": None,
            "user_id": None, "media_type": ("image" if not body else "chat")}


def _a(body):   # agente humano
    return {"from_me": True, "is_note": False, "body": body, "sent_from": "WEB",
            "user_id": "op-alex", "media_type": "chat"}


MIGUEL = [
    _c(""),  # [media]
    _c(""),  # [media]
    _a("Estoy siempre a la orden cuando desees me escribes y te atiendo de inmediato, "
       "ten bello día. Por aquí le dejo mi numero +593999303548"),
    _a("Te pase un numero por error este es el correcto +593989568605"),
]

EDUARDO = [
    _c("¿Cómo puedo obtener el Bono del 100% + $5 de regalo?🎁?"),
    _c("Si"),
    _a("Hola amigo, yo puedo ayudarte a crear tu cuenta o si deseas mas información me "
       "dices y te la brindo con todo el gusto. Procedo a enviarte el formulario amigo "
       "para tu registro Nombre de usuario: Correo: Numero tlf:"),
    _a("Hola amigo. Estoy siempre a la orden cuando desees me escribes y te atiendo de "
       "inmediato, ten bello día. Por aquí le dejo mi numero +593999303548"),
]


def run(name: str, msgs: list[dict], llm) -> None:
    stats = message_stats(msgs)
    status, reason = decide_eligibility(
        real_message_count=stats.message_count,
        customer_message_count=stats.contact_message_count,
        business_message_count=stats.agent_message_count + stats.bot_message_count,
        customer_text_count=stats.contact_text_message_count,
    )
    print(f"### {name}  (cliente={stats.contact_message_count}, "
          f"con texto={stats.contact_text_message_count}, agente={stats.agent_message_count})")
    if status != "evaluated":
        print(f"  ELEGIBILIDAD -> SKIP ({reason})  ✅ no se scorea\n")
        return
    rubric = decide_rubric(agent_message_count=stats.agent_message_count,
                           bot_message_count=stats.bot_message_count)
    res = score_conversation(rubric=rubric, target_messages=msgs, thread_context="", llm=llm)
    print(f"  SCORE -> {res.stars}★ {res.rating_label}")
    print(f"  {res.rating_rationale}")
    print(f"  dims: {res.dimensions}\n")


def main() -> None:
    cfg = load_config()
    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token, timeout=180.0)
    ok, msg = llm.check_model()
    print(f"[preflight] {'ok' if ok else 'FALLO'}: {msg}\n")
    run("MIGUEL (2 media + cierre)", MIGUEL, llm)
    run("EDUARDO (bono -> formulario, cliente no sigue)", EDUARDO, llm)


if __name__ == "__main__":
    main()
