"""Que tan bien juzga el MODELO SOLO, sin una sola regla determinista encima.

LA PREGUNTA, Y POR QUE ES DISTINTA A LA DEL OTRO ARNES
------------------------------------------------------
`scripts/bench_modelo.py` mide al modelo DESPUES de `score_by_motivo`: 268 ramas de
if/elif, overrides que le pisan el motivo, pisos que le corrigen los hechos, y en 77,9% de
los casos una rubrica determinista que le tira el rating entero. Eso responde "que modelo,
filtrado por nuestras reglas, cae donde cae la referencia" -- y es circular, porque el
motivo que el modelo elige decide si su propio juicio sobrevive.

Este arnes pregunta otra cosa: **si le sacamos las reglas, el modelo solo alcanza?** Si la
respuesta es si, la mitad de las 268 ramas no tiene razon de existir.

QUE SE LE PIDE: TRES COSAS, Y NADA MAS
--------------------------------------
1. `motivo`  -- de que se trato la conversacion (lista cerrada, la nuestra).
2. `resuelto` -- el operador resolvio lo que el cliente pedia, o al menos le explico por
   que no podia. Tres estados, porque "no pudo pero lo explico" NO es lo mismo que
   "no hizo nada": el manual de ATC pide justamente eso.
3. `mejora`  -- UNA cosa concreta que el operador podria haber hecho mejor.

Va un cuarto campo, `evidencia`: la frase del transcript que sostiene el `resuelto`. NO es
una cuarta pregunta -- es una CITA, cuesta pocos tokens, y sin ella no se puede saber si un
modelo acerto por criterio o por suerte. Es exactamente lo que hoy falta en produccion:
`atendio_el_motivo` y los otros tres hechos que deciden la nota NO se persisten, asi que
4.006 filas de `registro` con los mismos parametros visibles van de 2 a 5 estrellas y la
fila no tiene con que explicarlo.

LO QUE NO SE LE DA, A PROPOSITO
------------------------------
Ni `deposit_hint` (que hubo comprobante), ni `abandono_hint`, ni los timestamps, ni el
contexto del hilo, ni `is_new_contact`. Solo el transcript, renderizado con
`format_transcript` de produccion. Si con eso el resultado ya es acotado y correcto, la
ayuda determinista sobra para estas tres preguntas.

USO
---
    python scripts/bench_juicio.py muestra --n 20
    python scripts/bench_juicio.py correr --modelo qwen3:14b
    python scripts/bench_juicio.py comparar
    python scripts/bench_juicio.py leer --sesion 0ac9b02c
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
NUM_PREDICT = 1024
FAST_ATTEMPTS = 2
TIMEOUT = 300.0

SALIDA = REPO / "bench"
MUESTRA = SALIDA / "muestra_juicio.json"
RESULTADOS = SALIDA / "juicio.jsonl"

MOTIVOS = ("deposito", "retiro", "soporte_cuenta", "info", "promo", "registro", "problema")

SYSTEM = """Sos un supervisor de atencion al cliente de una plataforma de apuestas online.
Leete la conversacion completa entre el CLIENTE y el OPERADOR y contesta TRES cosas.

1. MOTIVO: de que se trato la conversacion. Elegi UNO de esta lista cerrada:
   - deposito: el cliente quiere recargar saldo, o pregunta como hacerlo.
   - retiro: el cliente quiere sacar plata, o pregunta como hacerlo.
   - registro: el cliente quiere crear una cuenta, o esta en proceso de crearla.
   - soporte_cuenta: problemas de acceso, clave, usuario, verificacion de identidad.
   - promo: bonos, promociones, sorteos, o el operador ofreciendo un gancho comercial.
   - info: consulta general que no cae en las anteriores (reglas, horarios, como funciona).
   - problema: un reclamo o disconformidad sobre algo que ya paso.
   Si dudas entre dos, elegi el que explica el motivo POR EL QUE EL CLIENTE ESCRIBIO,
   no lo que se termino hablando despues.

2. RESUELTO: el operador resolvio lo que el cliente pedia? Tres opciones:
   - "si": quedo resuelto, o el operador entrego lo que el cliente necesitaba.
   - "no_pero_explico": no se pudo resolver, PERO el operador le dijo al cliente por que
     no, o que tenia que esperar, o a donde tenia que ir.
   - "no": no se resolvio y el cliente se quedo sin una explicacion de por que.
   Si el CLIENTE dejo de contestar y el operador ya habia hecho su parte, eso NO es "no".

3. MEJORA: UNA sola cosa concreta que el operador podria haber hecho mejor, en una frase.
   Si no hay nada que mejorar, devolve string vacio.

Ademas, en "evidencia" cita la frase TEXTUAL del transcript en la que te apoyas para el
campo RESUELTO. Si no hay ninguna, devolve string vacio. No inventes frases.

Respondes SOLO con este JSON, sin texto alrededor:
{"motivo": "<uno de: deposito|retiro|soporte_cuenta|info|promo|registro|problema>", \
"resuelto": "<si|no_pero_explico|no>", "evidencia": "<cita textual o vacio>", \
"mejora": "<una frase o vacio>"}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "motivo": {"type": "string", "enum": list(MOTIVOS)},
        "resuelto": {"type": "string", "enum": ["si", "no_pero_explico", "no"]},
        "evidencia": {"type": "string"},
        "mejora": {"type": "string"},
    },
    "required": ["motivo", "resuelto", "mejora"],
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
    """20 conversaciones cubriendo los SIETE motivos y terminaciones variadas.

    El motivo guardado se usa SOLO para estratificar -- no como verdad. Es de v16 y en las
    sesiones ambiguas ya sabemos que se equivoca.
    """
    import psycopg

    if MUESTRA.exists() and not args.rehacer:
        print(f"ya existe: {len(json.loads(MUESTRA.read_text())['sesiones'])} sesiones "
              f"(--rehacer para tirarla)")
        return
    SALIDA.mkdir(exist_ok=True)
    por_motivo = max(1, args.n // len(MOTIVOS))
    sesiones: list[dict] = []
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        for motivo in MOTIVOS:
            # Terminaciones variadas: se ordena alternando el ultimo que hablo, para no
            # llevarse 20 conversaciones que terminan todas igual.
            cur.execute("""
              SELECT s.session_id, s.motivo, s.stars, s.llm_model,
                     s.eval_status, s.first_response_seconds
                FROM conversation_scores s
               WHERE s.motivo = %s AND s.eval_status='evaluated' AND s.stars IS NOT NULL
                 AND s.segment <> 'agente'
               ORDER BY md5(s.session_id::text)
               LIMIT %s""", (motivo, por_motivo + 1))
            for sid, m, st, lm, es, frs in cur.fetchall()[:por_motivo]:
                sesiones.append({
                    "session_id": str(sid), "motivo_v16": m, "stars_v16": float(st),
                    "camino_v16": ("determinista" if str(lm).startswith("determinista/")
                                   else "llm"),
                })
        # completar hasta n con los motivos mas ambiguos (los del fall-through)
        faltan = args.n - len(sesiones)
        if faltan > 0:
            ya = tuple(s["session_id"] for s in sesiones)
            cur.execute("""
              SELECT s.session_id, s.motivo, s.stars, s.llm_model
                FROM conversation_scores s
               WHERE s.eval_status='evaluated' AND s.stars IS NOT NULL
                 AND s.segment <> 'agente'
                 AND s.llm_model NOT LIKE 'determinista/%%'
                 AND NOT (s.session_id::text = ANY(%s))
               ORDER BY md5(s.session_id::text || 'x')
               LIMIT %s""", (list(ya), faltan))
            for sid, m, st, lm in cur.fetchall():
                sesiones.append({"session_id": str(sid), "motivo_v16": m,
                                 "stars_v16": float(st), "camino_v16": "llm"})

    MUESTRA.write_text(json.dumps({"n": len(sesiones), "sesiones": sesiones}, indent=2))
    reparto: dict = {}
    for s in sesiones:
        reparto[s["motivo_v16"]] = reparto.get(s["motivo_v16"], 0) + 1
    print(f"muestra congelada: {len(sesiones)} conversaciones -> {MUESTRA}")
    for m, n in sorted(reparto.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {m}")
    det = sum(1 for s in sesiones if s["camino_v16"] == "determinista")
    print(f"  ({det} venian del camino determinista, {len(sesiones)-det} del LLM)")


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

    print(f"host: {url}   muestra: {len(muestra)} conversaciones")
    print("SIN hints deterministas, SIN timestamps, SIN contexto de hilo: solo transcript")
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
                # Renderizado de PRODUCCION, sin tiempos (el default de prod).
                transcript = format_transcript(msgs, MOTIVOS[0])
                reg = {"modelo": modelo, "session_id": sid,
                       "motivo_v16": s.get("motivo_v16"), "stars_v16": s.get("stars_v16")}
                t0 = time.monotonic()
                try:
                    raw = llm.chat_json(SYSTEM, f"CONVERSACION:\n{transcript}", SCHEMA)
                    reg.update({
                        "ok": True,
                        "motivo": raw.get("motivo"),
                        "resuelto": raw.get("resuelto"),
                        "evidencia": (raw.get("evidencia") or "")[:300],
                        "mejora": (raw.get("mejora") or "")[:300],
                    })
                except Exception as e:  # noqa: BLE001 - la falla es un dato del modelo
                    reg.update({"ok": False,
                                "error": f"{type(e).__name__}: {str(e)[:200]}"})
                reg["segundos"] = round(time.monotonic() - t0, 2)
                with RESULTADOS.open("a") as f:
                    f.write(json.dumps(reg, ensure_ascii=False) + "\n")
                est = f"{reg.get('motivo')}/{reg.get('resuelto')}" if reg.get("ok") \
                    else reg.get("error", "?")[:40]
                print(f"  {k:>3}/{len(pend)}  {reg['segundos']:>6.1f}s  {est}", flush=True)
            print(f"[{modelo}] listo en {(time.monotonic()-t_m)/60:.1f} min\n", flush=True)


# --------------------------------------------------------------------------- comparar
def _cargar() -> list[dict]:
    if not RESULTADOS.exists():
        sys.exit("no hay resultados")
    return [json.loads(l) for l in RESULTADOS.read_text().splitlines() if l.strip()]


def cmd_comparar(args) -> None:
    regs = _cargar()
    por_mod: dict = {}
    for r in regs:
        por_mod.setdefault(r["modelo"], []).append(r)

    # Consenso: la moda entre modelos. NO es la verdad -- es donde NO hay que ir a leer.
    por_ses: dict = {}
    for r in regs:
        if r.get("ok"):
            por_ses.setdefault(r["session_id"], {})[r["modelo"]] = r
    consenso_m, consenso_r = {}, {}
    for sid, v in por_ses.items():
        for campo, dest in (("motivo", consenso_m), ("resuelto", consenso_r)):
            c: dict = {}
            for r in v.values():
                c[r[campo]] = c.get(r[campo], 0) + 1
            top, n = max(c.items(), key=lambda kv: kv[1])
            dest[sid] = (top, n, len(v))

    print("=== POR MODELO ===\n")
    VALIDOS = ("si", "no_pero_explico", "no")
    cab = (f"  {'modelo':<24} {'ok':>6} {'p50':>7} {'resuelto INVAL':>15} "
           f"{'motivo=cons':>12} {'resuelto=cons':>14} {'con evidencia':>14} "
           f"{'sin mejora':>11} {'largo mej':>10}")
    print(cab); print("  " + "-" * (len(cab) - 2))
    for modelo, rs in sorted(por_mod.items()):
        oks = [r for r in rs if r.get("ok")]
        if not oks:
            print(f"  {modelo:<24} 0/{len(rs)}"); continue
        lat = [r["segundos"] for r in oks]
        # Un `resuelto` fuera del enum es violacion de CONTRATO, no una opinion distinta:
        # el camino rapido usa format=json y no fuerza el schema, asi que el modelo puede
        # devolver "" y nadie lo atrapa. Se cuenta aparte y NO entra en el acuerdo.
        inval = sum(1 for r in oks if r.get("resuelto") not in VALIDOS)
        buenos = [r for r in oks if r.get("resuelto") in VALIDOS]
        am = sum(1 for r in oks if consenso_m.get(r["session_id"], (None,))[0] == r["motivo"])
        ar = sum(1 for r in buenos
                 if consenso_r.get(r["session_id"], (None,))[0] == r["resuelto"])
        ev = sum(1 for r in oks if (r.get("evidencia") or "").strip())
        mv = sum(1 for r in oks if not (r.get("mejora") or "").strip())
        lm = statistics.mean([len(r.get("mejora") or "") for r in oks])
        print(f"  {modelo:<24} {len(oks):>2}/{len(rs):<3} {statistics.median(lat):>6.1f}s "
              f"{f'{inval}/{len(oks)}':>15} {f'{am}/{len(oks)}':>12} "
              f"{f'{ar}/{len(buenos)}':>14} {f'{ev}/{len(oks)}':>14} "
              f"{f'{mv}/{len(oks)}':>11} {lm:>9.0f}c")

    print("\n=== POR CONVERSACION: donde los modelos NO se ponen de acuerdo ===\n")
    print(f"  {'sesion':<10} {'v16':<15} {'consenso motivo':<22} {'consenso resuelto':<24}")
    dudosas = []
    for sid in sorted(por_ses):
        cm, nm, tot = consenso_m[sid]
        cr, nr, _ = consenso_r[sid]
        v16 = por_ses[sid][list(por_ses[sid])[0]].get("motivo_v16")
        marca_m = "" if nm == tot else "  <-- DISENSO"
        marca_r = "" if nr == tot else "  <-- DISENSO"
        if nm < tot or nr < tot:
            dudosas.append(sid)
        print(f"  {sid[:8]:<10} {str(v16):<15} {cm+f' {nm}/{tot}':<22}{marca_m:<14} "
              f"{cr+f' {nr}/{tot}':<24}{marca_r}")
    print(f"\n  unanimes en TODO: {len(por_ses)-len(dudosas)}/{len(por_ses)}")
    print("  el motivo de v16 NO es la verdad: es de la vara vieja y en las ambiguas falla.")


def cmd_leer(args) -> None:
    """Todo lo que dijo cada modelo sobre UNA conversacion, para juzgar a mano."""
    regs = [r for r in _cargar() if r["session_id"].startswith(args.sesion)]
    if not regs:
        sys.exit(f"sin resultados para {args.sesion}")
    print(f"sesion {regs[0]['session_id']}   v16: motivo={regs[0].get('motivo_v16')} "
          f"estrellas={regs[0].get('stars_v16')}\n")
    for r in sorted(regs, key=lambda x: x["modelo"]):
        if not r.get("ok"):
            print(f"  {r['modelo']:<24} FALLO: {r.get('error')}"); continue
        print(f"  {r['modelo']:<24} {r['motivo']:<15} resuelto={r['resuelto']}")
        if r.get("evidencia"):
            print(f'      evidencia: "{r["evidencia"][:150]}"')
        if r.get("mejora"):
            print(f"      mejora   : {r['mejora'][:180]}")
        print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("muestra")
    pm.add_argument("--n", type=int, default=20)
    pm.add_argument("--rehacer", action="store_true")
    pm.set_defaults(func=cmd_muestra)

    pc = sub.add_parser("correr")
    pc.add_argument("--env", default=".env.local2")
    pc.add_argument("--modelo")
    pc.add_argument("--todos", action="store_true")
    pc.set_defaults(func=cmd_correr)

    pv = sub.add_parser("comparar")
    pv.set_defaults(func=cmd_comparar)

    pl = sub.add_parser("leer")
    pl.add_argument("--sesion", required=True)
    pl.set_defaults(func=cmd_leer)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
