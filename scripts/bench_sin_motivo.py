"""Puede el modelo reconocer que el cliente NO planteo nada?

LA PREGUNTA. `sin_motivo` saltea **5.247 sesiones**: el cliente solo saludo o agradecio, asi
que no hay motivo que clasificar. Hoy lo decide `signals.client_sin_motivo`, determinista. La
pregunta del negocio es si el modelo puede decir lo mismo -- porque si puede, el gate deja de
depender de un patron y la sesion se puede evaluar por lo unico que el manual pide ahi (el
estandar de cierre) en vez de desaparecer.

DOS TRAMPAS QUE ESTE ARNES EVITA, Y SIN ELLAS EL NUMERO NO VALE NADA

1. **La opcion de decir que no hay motivo tiene que EXISTIR en el enum.** Si se le ofrecen
   solo los siete motivos, el modelo elige uno siempre -- eso mide el prompt, no al modelo.
   Aca `sin_planteo` es una respuesta valida y de primera clase.

2. **HACE FALTA UN CONTROL.** Con una muestra de puras `sin_motivo`, un modelo que contestara
   "sin planteo" a todo sacaria 100%. La muestra es MIXTA y balanceada: mitad `sin_motivo`,
   mitad sesiones evaluadas con motivo real. Se mide en las dos direcciones -- reconocer la
   ausencia Y no verla donde no esta.

LA REFERENCIA NO ES LA VERDAD. `client_sin_motivo` es un patron determinista y puede
equivocarse: es justamente lo que se quiere saber. Cuando el modelo y la señal discrepan, la
unica forma de resolverlo es LEER el transcript -- para eso esta el subcomando `leer`.

USO
    python scripts/bench_sin_motivo.py muestra --n 40
    python scripts/bench_sin_motivo.py correr --modelo gemma4:12b
    python scripts/bench_sin_motivo.py comparar
    python scripts/bench_sin_motivo.py leer --sesion 0ac9b02c
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NUM_CTX = 16384
NUM_PREDICT = 512          # la respuesta es corta: enum + una cita
FAST_ATTEMPTS = 2
TIMEOUT = 300.0

SALIDA = REPO / "bench"
MUESTRA = SALIDA / "muestra_sin_motivo.json"
RESULTADOS = SALIDA / "sin_motivo.jsonl"

SYSTEM = """Sos un supervisor de atencion al cliente de una plataforma de apuestas online.
Leete la conversacion y contesta UNA sola cosa: el CLIENTE planteo algo concreto, o no?

"sin_planteo" cuando el cliente NO pidio ni pregunto nada: solo saludo ("hola", "buenas"),
solo agradecio ("gracias", "ok", "listo"), solo mando un emoji o sticker, o solo escribio
cortesias. No importa cuanto haya escrito el OPERADOR: la pregunta es sobre el CLIENTE.

"planteo" cuando el cliente pidio algo o pregunto algo concreto: una recarga, un retiro, una
consulta, un reclamo, ayuda con su cuenta, informacion de una promo, cualquier cosa que le de
al operador algo que hacer.

OJO: mandar un comprobante SIN texto es un planteo -- el cliente esta pidiendo que le
acrediten. Y un "gracias" DESPUES de que le resolvieron sigue siendo sin_planteo: lo que se
pregunta es si el cliente trajo un motivo a esta conversacion.

En "evidencia" cita la frase TEXTUAL del cliente en la que te apoyas. Si no hay ninguna
(porque solo mando media), devolve string vacio. No inventes frases.

Respondes SOLO con este JSON, sin texto alrededor:
{"planteo": "<planteo|sin_planteo>", "evidencia": "<cita textual del CLIENTE o vacio>"}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "planteo": {"type": "string", "enum": ["planteo", "sin_planteo"]},
        "evidencia": {"type": "string"},
    },
    "required": ["planteo"],
}


def _dsn() -> str:
    load_dotenv(REPO / ".env", override=False)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("falta DATABASE_URL (.env)")
    return dsn


def _host(env_file: str):
    load_dotenv(REPO / env_file, override=True)
    url = os.environ.get("OLLAMA_URL", "")
    token = os.environ.get("OLLAMA_TOKEN", "")
    if not url:
        sys.exit(f"falta OLLAMA_URL en {env_file}")
    from src.llm import OllamaClient

    c = OllamaClient(base_url=url, model="", token=token or None, timeout=30.0)
    return url, token, sorted(c.available_models())


# --------------------------------------------------------------------------- muestra
def cmd_muestra(args) -> None:
    """Muestra MIXTA y balanceada: la mitad son el caso, la mitad el control."""
    import psycopg

    if MUESTRA.exists() and not args.rehacer:
        print(f"ya existe: {len(json.loads(MUESTRA.read_text())['sesiones'])} sesiones")
        return
    SALIDA.mkdir(exist_ok=True)
    mitad = args.n // 2
    sesiones: list[dict] = []
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        # EL CASO: lo que hoy se saltea por `sin_motivo`.
        cur.execute("""SELECT session_id FROM conversation_scores
                        WHERE skip_reason='sin_motivo' AND session_id IS NOT NULL
                        ORDER BY md5(session_id::text) LIMIT %s""", (mitad,))
        for (sid,) in cur.fetchall():
            sesiones.append({"session_id": str(sid), "esperado_determinista": "sin_planteo"})
        # EL CONTROL: sesiones EVALUADAS, o sea con motivo real. Sin esto, un modelo que
        # contestara "sin_planteo" a todo sacaria 100%.
        cur.execute("""SELECT session_id FROM conversation_scores
                        WHERE eval_status='evaluated' AND motivo IS NOT NULL
                          AND session_id IS NOT NULL
                        ORDER BY md5(session_id::text || 'ctrl') LIMIT %s""", (mitad,))
        for (sid,) in cur.fetchall():
            sesiones.append({"session_id": str(sid), "esperado_determinista": "planteo"})

    MUESTRA.write_text(json.dumps({"n": len(sesiones), "sesiones": sesiones}, indent=2))
    caso = sum(1 for s in sesiones if s["esperado_determinista"] == "sin_planteo")
    print(f"muestra congelada: {len(sesiones)} sesiones -> {MUESTRA}")
    print(f"  {caso} del caso (hoy `sin_motivo`) + {len(sesiones)-caso} de control (con motivo)")


# --------------------------------------------------------------------------- correr
def _hechos() -> set:
    if not RESULTADOS.exists():
        return set()
    return {(json.loads(l)["modelo"], json.loads(l)["session_id"])
            for l in RESULTADOS.read_text().splitlines() if l.strip()}


def cmd_correr(args) -> None:
    import psycopg

    from src.context import fetch_session_messages
    from src.llm import OllamaClient
    from src.prompts import format_transcript

    if not MUESTRA.exists():
        sys.exit("no hay muestra: corre primero `muestra`")
    muestra = json.loads(MUESTRA.read_text())["sesiones"]
    url, token, disponibles = _host(args.env)
    modelos = disponibles if args.todos else ([args.modelo] if args.modelo else None)
    if not modelos:
        sys.exit("elegi --modelo <nombre> o --todos")
    faltan = [m for m in modelos if m not in disponibles]
    if faltan:
        sys.exit(f"no estan en el host: {faltan}")

    print(f"host: {url}   muestra: {len(muestra)} sesiones (mitad caso, mitad control)")
    print(f"modelos: {', '.join(modelos)}\n", flush=True)
    hechos = _hechos()
    SALIDA.mkdir(exist_ok=True)
    with psycopg.connect(_dsn()) as conn:
        for modelo in modelos:
            pend = [s for s in muestra if (modelo, s["session_id"]) not in hechos]
            if not pend:
                print(f"[{modelo}] completo, salteado")
                continue
            print(f"[{modelo}] {len(pend)} por medir", flush=True)
            llm = OllamaClient(base_url=url, model=modelo, token=token or None,
                               timeout=TIMEOUT, num_ctx=NUM_CTX,
                               num_predict=NUM_PREDICT, fast_attempts=FAST_ATTEMPTS)
            t_m = time.monotonic()
            for k, s in enumerate(pend, 1):
                sid = s["session_id"]
                with conn.cursor() as cur:
                    msgs = fetch_session_messages(cur, sid)
                transcript = format_transcript(msgs, "info")
                reg = {"modelo": modelo, "session_id": sid,
                       "esperado_determinista": s["esperado_determinista"]}
                t0 = time.monotonic()
                try:
                    raw = llm.chat_json(SYSTEM, f"CONVERSACION:\n{transcript}", SCHEMA)
                    reg.update({"ok": True, "planteo": raw.get("planteo"),
                                "evidencia": (raw.get("evidencia") or "")[:200]})
                except Exception as e:  # noqa: BLE001
                    reg.update({"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
                reg["segundos"] = round(time.monotonic() - t0, 2)
                with RESULTADOS.open("a") as f:
                    f.write(json.dumps(reg, ensure_ascii=False) + "\n")
                marca = "=" if reg.get("planteo") == s["esperado_determinista"] else "X"
                print(f"  {k:>3}/{len(pend)} {reg['segundos']:>6.1f}s {marca} "
                      f"{reg.get('planteo') or reg.get('error','?')[:30]}", flush=True)
            print(f"[{modelo}] listo en {(time.monotonic()-t_m)/60:.1f} min\n", flush=True)


# --------------------------------------------------------------------------- comparar
def cmd_comparar(args) -> None:
    if not RESULTADOS.exists():
        sys.exit("no hay resultados")
    regs = [json.loads(l) for l in RESULTADOS.read_text().splitlines() if l.strip()]
    por: dict = {}
    for r in regs:
        por.setdefault(r["modelo"], []).append(r)

    print("La señal determinista `client_sin_motivo` NO es la verdad: es la referencia. Las")
    print("discrepancias son la lista de trabajo -- hay que LEER el transcript.\n")
    cab = (f"  {'modelo':<22} {'ok':>7} {'p50':>7} {'ve la ausencia':>15} "
           f"{'no la inventa':>14} {'acuerdo':>9}")
    print(cab); print("  " + "-" * (len(cab) - 2))
    for modelo, rs in sorted(por.items()):
        oks = [r for r in rs if r.get("ok")]
        if not oks:
            print(f"  {modelo:<22} 0/{len(rs)}"); continue
        caso = [r for r in oks if r["esperado_determinista"] == "sin_planteo"]
        ctrl = [r for r in oks if r["esperado_determinista"] == "planteo"]
        # recall sobre el caso: cuantas ausencias reconoce
        ve = sum(1 for r in caso if r["planteo"] == "sin_planteo")
        # especificidad sobre el control: cuantas veces NO inventa una ausencia
        no_inventa = sum(1 for r in ctrl if r["planteo"] == "planteo")
        ac = sum(1 for r in oks if r["planteo"] == r["esperado_determinista"])
        lat = [r["segundos"] for r in oks]
        print(f"  {modelo:<22} {len(oks):>3}/{len(rs):<3} "
              f"{statistics.median(lat):>6.1f}s "
              f"{f'{ve}/{len(caso)}':>15} {f'{no_inventa}/{len(ctrl)}':>14} "
              f"{f'{ac}/{len(oks)}':>9}")

    print("\n=== DISCREPANCIAS: donde el modelo y la señal no coinciden ===")
    for modelo, rs in sorted(por.items()):
        malas = [r for r in rs if r.get("ok") and r["planteo"] != r["esperado_determinista"]]
        if not malas:
            continue
        print(f"\n  {modelo}: {len(malas)}")
        for r in malas[:8]:
            print(f"    {r['session_id'][:8]}  señal={r['esperado_determinista']:<12} "
                  f"modelo={r['planteo']:<12} ev={r.get('evidencia','')[:60]!r}")


def cmd_leer(args) -> None:
    import psycopg

    from src.context import fetch_session_messages
    from src.prompts import format_transcript
    from src.signals import client_sin_motivo

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM conversations WHERE id::text LIKE %s LIMIT 1",
                    (args.sesion + "%",))
        row = cur.fetchone()
        if not row:
            sys.exit(f"no existe {args.sesion}")
        msgs = fetch_session_messages(cur, row[0])
    print(f"client_sin_motivo (la señal): {client_sin_motivo(msgs)}")
    if RESULTADOS.exists():
        for l in RESULTADOS.read_text().splitlines():
            r = json.loads(l)
            if r["session_id"].startswith(args.sesion):
                print(f"  {r['modelo']:<22} {r.get('planteo')}  ev={r.get('evidencia','')[:70]!r}")
    print("-" * 70)
    print(format_transcript(msgs, "info"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pm = sub.add_parser("muestra"); pm.add_argument("--n", type=int, default=40)
    pm.add_argument("--rehacer", action="store_true"); pm.set_defaults(func=cmd_muestra)
    pc = sub.add_parser("correr"); pc.add_argument("--env", default=".env.local2")
    pc.add_argument("--modelo"); pc.add_argument("--todos", action="store_true")
    pc.set_defaults(func=cmd_correr)
    pv = sub.add_parser("comparar"); pv.set_defaults(func=cmd_comparar)
    pl = sub.add_parser("leer"); pl.add_argument("--sesion", required=True)
    pl.set_defaults(func=cmd_leer)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
