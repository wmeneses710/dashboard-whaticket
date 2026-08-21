"""Benchmark de modelos de LLM para el pase de scoring, sobre las filas del fall-through.

POR QUE EL FALL-THROUGH Y NO UNA MUESTRA CUALQUIERA
---------------------------------------------------
El LLM se invoca en 38.777 sesiones pero su RATING sobrevive en 8.575 (22,1%): en el camino
determinista (`llm_model = 'determinista/*'`) las rubricas de deposito/retiro/registro/promo/
soporte/info pisan la nota y del modelo solo sobrevive el `motivo`. Medir un modelo sobre esas
filas mide casi nada. La muestra sale de `conversation_scores WHERE llm_model NOT LIKE
'determinista/%%'`, que son exactamente las filas donde el veredicto del modelo ES la nota.

TODO SALE DEL CODIGO DE PRODUCCION
----------------------------------
Las funciones Y la carga de datos. Un `SELECT` propio es una reimplementacion silenciosa del
contrato: `fetch_session_messages` trae `m.ack`, y sin esa columna `_cliente_lo_leyo` degrada a
True y apaga el techo de `registro`. Este arnes espeja `worker.score_session_and_store` paso a
paso: `evaluate_session`, `deposit_candidate_count`, `build_lineas_map`, `score_by_motivo`.

LA MUESTRA ES PAREADA Y DETERMINISTA
------------------------------------
Se congela una vez en `muestra.json` (orden por `md5(session_id)`, estable entre corridas) y
TODOS los modelos ven las MISMAS sesiones. Sin esto, dos modelos con muestras distintas no se
comparan: se comparan las muestras.

LA REFERENCIA NO ES LA FILA GUARDADA
------------------------------------
`conversation_scores` se calculo con codigo viejo (entre otras cosas con `AGIL=2`, hoy 1). Con
cambios sin commitear encima, la fila guardada YA NO ES BASELINE: comparar contra ella devuelve
el delta de las versiones nuevas disfrazado de desacuerdo del modelo. Ya nos costo un "19,66%
de ruido" que era el delta de v18. La referencia es el modelo de produccion (`--referencia`)
corrido AHORA, con este mismo codigo, sobre esta misma muestra.

LA CANCHA ES LA MISMA PARA TODOS
--------------------------------
`num_ctx`, `num_predict` y `fast_attempts` se fijan ACA, no se leen del entorno: si un modelo
corre con otro presupuesto de tokens, lo que se mide es el presupuesto. El piso de
`num_predict` existe porque con el default de produccion (768) un modelo verborragico trunca el
JSON y pierde por locuacidad, no por criterio.

EL TIMEOUT MIDE AL HOST, NO AL MODELO
-------------------------------------
Este host (192.168.100.183) es ~7x mas lento que produccion con el MISMO qwen3:14b (p50 43,5s
vs 6,0s). Un 27B que se pasa de los 180s de produccion ACA no dice nada sobre el 27B en
hardware decente. Por eso el timeout del arnes es generoso (300s por defecto): sirve para
sacarle la señal de CALIDAD. La latencia se reporta aparte, contra el presupuesto real de
produccion, y son dos preguntas distintas: cual modelo juzga mejor, y si este host sirve.

USO
---
    python scripts/bench_modelo.py muestra --n 12
    python scripts/bench_modelo.py correr --modelo qwen3:14b
    python scripts/bench_modelo.py correr --todos
    python scripts/bench_modelo.py resumen --referencia qwen3:14b

Es RESUMIBLE: cada sesion se appendea a `resultados.jsonl` y una corrida repetida saltea los
pares (modelo, sesion) ya hechos. Una corrida cortada no se pierde.
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

# La cancha, igual para todos los modelos (ver docstring).
NUM_CTX = 16384
NUM_PREDICT = 1024          # piso: el default de prod (768) trunca a los verborragicos
FAST_ATTEMPTS = 2           # el default de produccion (LLM_FAST_ATTEMPTS)
TIMEOUT = 300.0             # generoso a proposito: mide calidad, no el host

SALIDA = REPO / "bench"
MUESTRA = SALIDA / "muestra.json"
RESULTADOS = SALIDA / "resultados.jsonl"


def _dsn() -> str:
    load_dotenv(REPO / ".env", override=False)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("falta DATABASE_URL (.env)")
    return dsn


def _host_modelos(env_file: str) -> tuple[str, str, list[str]]:
    """(url, token, modelos) del host bajo prueba. Nunca imprime el token."""
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
    """Congela la muestra pareada. Se corre UNA vez; despues se reusa tal cual."""
    import psycopg

    if MUESTRA.exists() and not args.rehacer:
        m = json.loads(MUESTRA.read_text())
        print(f"la muestra ya existe: {len(m['sesiones'])} sesiones "
              f"(--rehacer para tirarla)")
        return
    SALIDA.mkdir(exist_ok=True)
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        # Las filas del fall-through: el rating del modelo ES la nota. `motivo` viaja solo
        # para estratificar y para leer el resumen, NO como referencia (es de codigo viejo).
        cur.execute(
            """SELECT s.session_id, s.motivo, s.account
                 FROM conversation_scores s
                WHERE s.llm_model IS NOT NULL
                  AND s.llm_model NOT LIKE 'determinista/%%'
                  AND s.session_id IS NOT NULL
                  AND s.eval_status = 'evaluated'
                ORDER BY md5(s.session_id::text)""")
        filas = cur.fetchall()
    print(f"universo del fall-through: {len(filas)} sesiones")

    # Estratificado por motivo: sin esto la muestra la domina el motivo mas frecuente y un
    # modelo puede ganar por acertarle a uno solo.
    porm: dict[str, list] = {}
    for sid, motivo, account in filas:
        porm.setdefault(motivo or "(sin motivo)", []).append((str(sid), account))
    motivos = sorted(porm, key=lambda k: -len(porm[k]))
    sesiones: list[dict] = []
    i = 0
    while len(sesiones) < args.n:
        avance = False
        for m in motivos:
            if i < len(porm[m]) and len(sesiones) < args.n:
                sid, account = porm[m][i]
                sesiones.append({"session_id": sid, "account": account, "motivo_viejo": m})
                avance = True
        if not avance:
            break
        i += 1

    MUESTRA.write_text(json.dumps({
        "n": len(sesiones),
        "criterio": "fall-through (llm_model no determinista), md5(session_id), "
                    "round-robin por motivo",
        "sesiones": sesiones,
    }, indent=2))
    reparto: dict[str, int] = {}
    for s in sesiones:
        reparto[s["motivo_viejo"]] = reparto.get(s["motivo_viejo"], 0) + 1
    print(f"muestra congelada en {MUESTRA}: {len(sesiones)} sesiones")
    for m, n in sorted(reparto.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {m}")


# --------------------------------------------------------------------------- correr
def _hechos() -> set[tuple[str, str, str]]:
    """Tripletas (host, modelo, session_id) ya medidas, para resumir una corrida cortada.

    EL HOST ENTRA EN LA CLAVE. Sin el, correr el MISMO modelo en otro endpoint se saltea
    como "ya hecho" -- y justo esa es la comparacion que aisla el HARDWARE: mismo modelo,
    misma muestra, misma cancha, distinto servidor. Las corridas viejas no traen `env`, asi
    que se les asigna el default para que sigan contando.
    """
    if not RESULTADOS.exists():
        return set()
    hechos = set()
    for line in RESULTADOS.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            hechos.add((r.get("env", ".env.local2"), r["modelo"], r["session_id"]))
    return hechos


def _contexto_de_sesion(cur, session_id: str) -> dict | None:
    """Los campos del envase que `score_session_and_store` le pasa a la rubrica.

    Se leen de `conversations` (como en produccion, PENDING_SESSIONS_SQL une
    c.id = cs.session_id), no de `conversation_scores`.
    """
    cur.execute(
        """SELECT c.resolved_at, q.name
             FROM conversations c
             LEFT JOIN queues q ON q.id = c.queue_id
            WHERE c.id = %s""", (session_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"resolved_at": row[0], "queue_name": row[1]}


def cmd_correr(args) -> None:
    import psycopg

    from src.context import fetch_session_messages
    from src.deposits import deposit_candidate_count
    from src.llm import OllamaClient
    from src.redireccion import (
        build_lineas_map,
        respuesta_fue_solo_traspaso,
        score_redireccion,
    )
    from src.scorer import score_by_motivo
    from src.segments import segment_for_queue
    from src.sessions import evaluate_session

    if not MUESTRA.exists():
        sys.exit("no hay muestra: corre primero `muestra`")
    muestra = json.loads(MUESTRA.read_text())["sesiones"]
    url, token, disponibles = _host_modelos(args.env)

    if args.todos:
        modelos = disponibles
    elif args.modelo:
        modelos = [args.modelo]
    else:
        sys.exit("elegi --modelo <nombre> o --todos")
    faltan = [m for m in modelos if m not in disponibles]
    if faltan:
        sys.exit(f"no estan en el host: {faltan}\ndisponibles: {disponibles}")

    print(f"host: {url}  ({len(disponibles)} modelos)")
    print(f"muestra: {len(muestra)} sesiones del fall-through, pareada")
    print(f"cancha:  num_ctx={NUM_CTX} num_predict={NUM_PREDICT} "
          f"fast_attempts={FAST_ATTEMPTS} timeout={TIMEOUT:.0f}s")
    print(f"modelos: {', '.join(modelos)}\n", flush=True)

    hechos = _hechos()
    SALIDA.mkdir(exist_ok=True)
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            lineas = build_lineas_map(cur)

        for modelo in modelos:
            pendientes = [s for s in muestra
                          if (args.env, modelo, s["session_id"]) not in hechos]
            if not pendientes:
                print(f"[{modelo}] ya estaba completo, salteado")
                continue
            print(f"[{modelo}] {len(pendientes)} sesiones por medir", flush=True)
            llm = _Espia(OllamaClient(
                base_url=url, model=modelo, token=token or None,
                timeout=TIMEOUT, num_ctx=NUM_CTX,
                num_predict=NUM_PREDICT, fast_attempts=FAST_ATTEMPTS))
            t_modelo = time.monotonic()
            for k, s in enumerate(pendientes, 1):
                sid = s["session_id"]
                with conn.cursor() as cur:
                    msgs = fetch_session_messages(cur, sid)
                    ctx = _contexto_de_sesion(cur, sid)
                if ctx is None:
                    continue
                # Mismo orden que worker.score_session_and_store.
                _stats, _rubric, eval_status, skip_reason = evaluate_session(
                    msgs, lineas=lineas)
                if eval_status != "evaluated":
                    # La sesion ya no es evaluable con el codigo de HOY (las reglas de skip
                    # cambiaron desde que se guardo la fila). No es culpa del modelo.
                    _apendar({"env": args.env, "modelo": modelo, "session_id": sid,
                              "ok": False, "motivo_arnes": f"skip:{skip_reason}",
                              "segundos": 0.0})
                    continue
                if segment_for_queue(ctx["queue_name"]) == "agente":
                    # `agente` no pasa por el LLM (agilidad determinista). No deberia caer
                    # aca porque la muestra es del fall-through, pero si la cola cambio, si.
                    _apendar({"env": args.env, "modelo": modelo, "session_id": sid,
                              "ok": False, "motivo_arnes": "skip:agente",
                              "segundos": 0.0})
                    continue

                if respuesta_fue_solo_traspaso(msgs):
                    # ESPEJA EL WORKER (2026-08-20): `redireccion` se califica determinista
                    # y SIN LLM. Si el arnes llamara a score_by_motivo aca, mediria un
                    # camino que produccion ya no recorre -- y le cobraria al modelo una
                    # sesion que nunca va a ver. Ver src/worker.score_session_and_store.
                    det = score_redireccion(msgs, lineas)
                    _apendar({"env": args.env, "modelo": modelo, "session_id": sid,
                              "ok": False, "motivo_arnes": "determinista:redireccion",
                              "stars_determinista": float(det.stars) if det else None,
                              "segundos": 0.0})
                    continue

                antes = dict(llm.calls)
                t0 = time.monotonic()
                llm.crudo = None
                reg: dict = {"env": args.env, "modelo": modelo, "session_id": sid,
                             "motivo_viejo": s.get("motivo_viejo")}
                try:
                    res = score_by_motivo(
                        target_messages=msgs, thread_context="", llm=llm,
                        deposit_hint=deposit_candidate_count(msgs) > 0,
                        cierre_at=ctx["resolved_at"], lineas=lineas)
                    reg.update({
                        "ok": True,
                        "motivo": res.motivo,
                        "stars": float(res.stars),
                        "rating_label": res.rating_label,
                        "claridad": res.claridad,
                        "llm_model_efectivo": res.llm_model,
                        "errores": len(res.dimensions.get("errores") or []),
                    })
                except Exception as e:  # noqa: BLE001 - la falla ES un dato del modelo
                    reg.update({"ok": False,
                                "motivo_arnes": f"{type(e).__name__}: {str(e)[:200]}"})
                reg["segundos"] = round(time.monotonic() - t0, 2)
                reg["camino"] = {k2: llm.calls[k2] - antes[k2] for k2 in llm.calls}
                # EL JUICIO CRUDO, antes de los overrides. Sin esto no se puede saber si la
                # cadena de reglas mejora o degrada lo que el modelo dijo.
                if llm.crudo is not None:
                    c = llm.crudo
                    dims = c.get("dimensions") if isinstance(c.get("dimensions"), dict) else {}
                    reg["crudo"] = {
                        "motivo": c.get("motivo"),
                        "atendio_el_motivo": c.get("atendio_el_motivo"),
                        "hizo_accion_extra": c.get("hizo_accion_extra"),
                        "cortesia_destacada": c.get("cortesia_destacada"),
                        "hubo_maltrato_grave": c.get("hubo_maltrato_grave"),
                        "claridad": c.get("claridad"),
                        "cliente_reinsistio": c.get("cliente_reinsistio"),
                        "atencion": c.get("atencion"),
                        "errores": (dims.get("errores") or []),
                    }
                _apendar(reg)
                estado = reg.get("motivo") or reg.get("motivo_arnes", "?")
                print(f"  {k:>3}/{len(pendientes)}  {reg['segundos']:>6.1f}s  {estado}",
                      flush=True)
            mins = (time.monotonic() - t_modelo) / 60
            print(f"[{modelo}] listo en {mins:.1f} min\n", flush=True)
    print(f"resultados en {RESULTADOS}")


def _apendar(reg: dict) -> None:
    with RESULTADOS.open("a") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


class _Espia:
    """Envuelve al cliente y guarda la salida CRUDA del modelo, antes de que la toque
    ninguna regla.

    POR QUE EXISTE. `score_by_motivo` aplica overrides deterministas sobre lo que el modelo
    dijo: pisa el motivo (guard deposito/retiro, alta cerrada -> registro), levanta pisos
    (PIEZA 1), descarta el maltrato sin evidencia dura, y en 77,9% de los casos tira el
    rating entero y usa la rubrica. Medir DESPUES de eso no responde "que modelo juzga
    mejor": responde "que modelo, filtrado por nuestras reglas, cae donde cae la referencia".
    Son dos preguntas distintas y hacen falta las dos.

    Espia en vez de reimplementar: el prompt, el schema y el parseo siguen siendo los de
    produccion. Una llamada, dos señales -- el juicio crudo y el veredicto final -- sin
    pagar inferencia de mas.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.crudo: dict | None = None

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def calls(self) -> dict:
        return self._inner.calls

    def chat_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        raw = self._inner.chat_json(system, user, schema)
        self.crudo = raw
        return raw


# --------------------------------------------------------------------------- resumen
def cmd_resumen(args) -> None:
    if not RESULTADOS.exists():
        sys.exit("no hay resultados todavia")
    por_modelo: dict[str, list[dict]] = {}
    for line in RESULTADOS.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            por_modelo.setdefault(r["modelo"], []).append(r)

    ref = args.referencia
    ref_motivo = {r["session_id"]: r.get("motivo")
                  for r in por_modelo.get(ref, []) if r.get("ok")}
    ref_stars = {r["session_id"]: r.get("stars")
                 for r in por_modelo.get(ref, []) if r.get("ok")}
    if not ref_motivo:
        print(f"AVISO: la referencia '{ref}' no tiene corridas OK — el acuerdo queda vacio. "
              f"Corre primero `correr --modelo {ref}`.\n")

    print(f"referencia: {ref} (corrida con ESTE codigo, no la fila guardada)\n")
    cab = (f"{'modelo':<24} {'ok':>7} {'p50':>7} {'p90':>7} {'ses/min':>8} "
           f"{'motivo=':>8} {'estr=':>7} {'|Δestr|':>8} {'fallback':>9} {'vacio':>6}")
    print(cab)
    print("-" * len(cab))
    for modelo, rs in sorted(por_modelo.items()):
        oks = [r for r in rs if r.get("ok")]
        lat = [r["segundos"] for r in oks]
        comunes = [r for r in oks if r["session_id"] in ref_motivo]
        ac_m = sum(1 for r in comunes if r["motivo"] == ref_motivo[r["session_id"]])
        ac_e = sum(1 for r in comunes
                   if r["stars"] == ref_stars.get(r["session_id"]))
        deltas = [abs(r["stars"] - ref_stars[r["session_id"]]) for r in comunes
                  if ref_stars.get(r["session_id"]) is not None]
        fb = sum(r.get("camino", {}).get("fallback", 0) for r in rs)
        vac = sum(r.get("camino", {}).get("empty", 0) for r in rs)
        p50 = statistics.median(lat) if lat else 0.0
        p90 = (statistics.quantiles(lat, n=10)[8] if len(lat) >= 10
               else (max(lat) if lat else 0.0))
        spm = (60.0 / statistics.mean(lat)) if lat else 0.0
        print(f"{modelo:<24} {len(oks):>3}/{len(rs):<3} {p50:>6.1f}s {p90:>6.1f}s "
              f"{spm:>8.1f} {f'{ac_m}/{len(comunes)}':>8} {f'{ac_e}/{len(comunes)}':>7} "
              f"{(statistics.mean(deltas) if deltas else 0):>8.2f} {fb:>9} {vac:>6}")

    print("\n-- fallas duras (no dieron nota) --")
    hubo = False
    for modelo, rs in sorted(por_modelo.items()):
        malas = [r for r in rs if not r.get("ok")
                 and not str(r.get("motivo_arnes", "")).startswith("skip:")]
        if malas:
            hubo = True
            print(f"  {modelo}: {len(malas)}/{len(rs)}")
            for r in malas[:4]:
                print(f"      {r['session_id'][:8]}  {r.get('motivo_arnes')}")
    if not hubo:
        print("  ninguna")

    print(f"\nRECORDATORIO: el p50 de este host no es el de produccion (mismo qwen3:14b, "
          f"43,5s vs 6,0s). La latencia de arriba mide el HOST; el acuerdo mide el MODELO.")


def cmd_crudo(args) -> None:
    """El juicio CRUDO del modelo, sin las reglas encima. Dos preguntas:

    1. Cuanto le pisa la cadena de reglas al modelo (crudo != final).
    2. En que coinciden los modelos ENTRE SI, sin asumir que produccion tiene razon.
       El consenso NO es la verdad -- es donde hay que ir a leer transcripts --, pero es
       mejor referencia que "lo que dijo el 14b", que en las sesiones ambiguas es outlier.
    """
    if not RESULTADOS.exists():
        sys.exit("no hay resultados todavia")
    regs = [json.loads(l) for l in RESULTADOS.read_text().splitlines() if l.strip()]
    con_crudo = [r for r in regs if r.get("crudo") and r.get("ok")]
    if not con_crudo:
        sys.exit("ninguna corrida tiene el juicio crudo: volve a correr `correr` "
                 "(los resultados viejos son de antes del espia)")

    print("=== 1. CUANTO LE PISA LA CADENA DE REGLAS AL MODELO ===\n")
    print(f"  {'modelo':<24} {'n':>4} {'motivo pisado':>14} {'estrella del LLM tirada':>24}")
    por: dict[str, list] = {}
    for r in con_crudo:
        por.setdefault(r["modelo"], []).append(r)
    for modelo, rs in sorted(por.items()):
        pisado = sum(1 for r in rs if r["crudo"].get("motivo") != r.get("motivo"))
        # rating del LLM descartado = la nota la puso una rubrica determinista
        tirada = sum(1 for r in rs
                     if str(r.get("llm_model_efectivo", "")).startswith("determinista/"))
        print(f"  {modelo:<24} {len(rs):>4} {pisado:>14} {tirada:>24}")

    print("\n=== 2. CONSENSO CRUDO POR SESION (sin referencia privilegiada) ===\n")
    por_ses: dict[str, dict] = {}
    for r in con_crudo:
        por_ses.setdefault(r["session_id"], {})[r["modelo"]] = r["crudo"].get("motivo")
    print(f"  {'sesion':<10} {'modelos':>8} {'consenso':<16} {'acuerdo':>8}  disidentes")
    unanimes = 0
    for sid, v in sorted(por_ses.items()):
        cuenta: dict[str, int] = {}
        for m in v.values():
            cuenta[m] = cuenta.get(m, 0) + 1
        top, n = max(cuenta.items(), key=lambda kv: kv[1])
        if n == len(v):
            unanimes += 1
        disidentes = ", ".join(f"{mo}={mv}" for mo, mv in sorted(v.items()) if mv != top)
        print(f"  {sid[:8]:<10} {len(v):>8} {str(top):<16} {n}/{len(v):<6} {disidentes[:60]}")
    print(f"\n  unanimes: {unanimes}/{len(por_ses)} sesiones")
    print("\n  Las sesiones SIN unanimidad son la lista de trabajo: ahi hay que leer el "
          "transcript\n  y decidir si el problema es el modelo, la tabla de motivos o la "
          "unidad juzgada.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("muestra", help="congelar la muestra pareada")
    pm.add_argument("--n", type=int, default=12,
                    help="sesiones (12 = el mismo N de la corrida anterior, comparable)")
    pm.add_argument("--rehacer", action="store_true")
    pm.set_defaults(func=cmd_muestra)

    pc = sub.add_parser("correr", help="medir uno o todos los modelos del host")
    pc.add_argument("--env", default=".env.local2")
    pc.add_argument("--modelo")
    pc.add_argument("--todos", action="store_true")
    pc.set_defaults(func=cmd_correr)

    pk = sub.add_parser("crudo", help="el juicio del modelo SIN las reglas encima")
    pk.set_defaults(func=cmd_crudo)

    pr = sub.add_parser("resumen", help="tabla comparativa")
    pr.add_argument("--referencia", default="qwen3:14b",
                    help="el modelo de produccion, corrido con este mismo codigo")
    pr.set_defaults(func=cmd_resumen)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
