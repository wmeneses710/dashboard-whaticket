#!/usr/bin/env python3
"""Simula las DOS reglas de gap sobre los mismos datos y reparte los cambios en dos baldes.

PARA QUE. El gap de sesionizacion paso de medirse entre created_at (nacimiento a
nacimiento) a medirse por INACTIVIDAD real (last_at del previo -> first_at del siguiente).
Ese cambio se apoya en `messages` agrupado por `conversation_id`, que es la columna que el
ETL atribuye mal en una fraccion grande de la cuenta `sistemas`. Es el MISMO dato por el
que el SPAN se dejo a proposito sobre created_at.

La pregunta que contesta este script, con numeros: de todas las fronteras cuya decision
CAMBIO al pasar a inactividad, cuantas cambiaron por actividad REAL y cuantas por
mensajes que no pertenecen a ese episodio?

    LIMPIO = los dos timestamps caen dentro de la ventana de vida de su propio episodio
             (created_at -> resolved_at). El fix hace lo que promete.
    SUCIO  = alguno cae fuera. La frontera se movio por el bug del ETL, no por la
             interaccion real.

EL RIESGO CONCRETO que se busca. Si un episodio previo absorbio mensajes posteriores a su
resolucion, su last_at queda adelantado, el silencio sale NEGATIVO, nunca supera GAP y la
frontera MERGEA. Eso reintroduce el over-merge que la regla de cierre habia resuelto, por
otra puerta. El balde SUCIO con direccion "despues" en el previo es exactamente esa
poblacion; el script la reporta aparte.

    python scripts/sim_sesiones_gap.py                      # las dos cuentas
    python scripts/sim_sesiones_gap.py --cuenta sistemas
    python scripts/sim_sesiones_gap.py --ejemplos 10        # casos concretos de cada balde
    python scripts/sim_sesiones_gap.py --tolerancia-min 30  # margen del borde de ventana

COPIA A MEDIO SANAR: MEDIR LAS DOS POBLACIONES POR SEPARADO. El re-heal del ETL avanza
por tramos, asi que en una copia a medio sanar conviven datos sanados y datos viejos.
Medirlos juntos da un promedio que no contesta nada. Dos corridas contestan si el re-heal
arregla la atribucion:

    python scripts/sim_sesiones_gap.py --cuenta sistemas --desde 2026-07-30   # sanado
    python scripts/sim_sesiones_gap.py --cuenta sistemas --hasta 2026-07-29   # sin sanar

    sanado LIMPIO + viejo SUCIO  -> el re-heal funciona; esperar a que termine y remedir.
    los dos SUCIOS               -> el re-heal no arregla la atribucion; esperar no sirve
                                    y el gap por inactividad no puede entrar.

El filtro toma el ticket COMPLETO (ver filtrar_tickets_completos): un ticket a caballo del
borde se excluye entero y se informa, porque recortarle episodios fabricaria fronteras que
no existen.

SOLO LEE. La conexion se abre read-only y nunca commitea: no toca conversation_sessions
ni conversation_session_map. Correrlo NO re-materializa nada.

COMO SE OBTIENE LA REGLA VIEJA SIN DUPLICAR LOGICA. assign_sessions cae a created_at
cuando el episodio no trae first_at/last_at (helper _actividad). Entonces la regla vieja
es la MISMA funcion con los episodios sin ventana de actividad: cero riesgo de que la
copia de la regla se desincronice de la real.

LIMITACION DECLARADA. Los flips se atribuyen a nivel frontera mirando solo el termino gap
(ver es_flip_de_gap en src/atribucion.py): el SPAN queda afuera porque depende de la
cadena de decisiones previas, que difiere entre reglas. El efecto TOTAL — span y cascadas
incluidas — se mide con la corrida completa de assign_sessions, que es el bloque
"efecto total" del reporte. Los dos numeros contestan preguntas distintas y no tienen por
que coincidir.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from src.atribucion import (  # noqa: E402
    DESPUES,
    comparar_reglas,
    con_piso_de_mensajes,
    filtrar_tickets_completos,
    es_flip_de_gap,
    es_sucia,
    percentiles,
    resumen,
)
from src.config import load_config  # noqa: E402
from src.sessions import (  # noqa: E402
    _LAST_AGENT_SQL,
    _LAST_MSG_SQL,
    _PRIMARY_AGENT_SQL,
    GAP,
)

# Mismo guard que scripts/snapshot_scores.py. Aca no destruye nada (solo lee), pero el
# escaneo completo de `messages` es caro: no conviene dispararlo contra produccion por
# accidente.
PISTAS_DE_COPIA = ("copia", "copy", "local", "test", "dev", "stage")

# Episodios de la cuenta + resolved_at, que es el techo de la ventana propia del episodio.
# Mismo ORDER BY y mismos tiebreakers que _CONVERSATIONS_SQL de src/sessions.py.
_CONVS_SQL = """
SELECT ticket_id, id, created_at, resolved_at
  FROM conversations
 WHERE account = %(account)s AND ticket_id IS NOT NULL
 ORDER BY ticket_id, created_at ASC, id ASC
"""

# Umbrales del veredicto. Son un CRITERIO, no una medicion.
#
# EL GO/NO-GO VA SOBRE EL RADIO DE IMPACTO (sucios / TOTAL de fronteras), no sobre el
# ratio sucios/flips. Por que: el ratio sobre flips mide la CALIDAD del fix (de lo que
# cambia, cuanto lo mueven datos podridos) y con pocos flips es ruidoso — 33 de 434 da
# 7,6% y suena a zona gris, pero esos 33 son el 0,03% de 109.406 fronteras, o sea nada.
# La pregunta del go/no-go es otra: cuantas fronteras del universo quedarian mal si se
# re-materializa. Eso es el radio de impacto.
UMBRAL_RADIO_SANO = 0.1      # % de fronteras sucias sobre el total
UMBRAL_RADIO_BLOQUEA = 1.0
# Sobre flips: solo para comentar la calidad del fix, no decide.
UMBRAL_CALIDAD_POBRE = 25.0


def _episodios_por_ticket(cur, cuenta: str, msgs_desde: datetime | None) -> dict:
    """Arma los episodios por ticket, igual que refresh_account_sessions, + resolved_at.

    msgs_desde aplica el piso de fecha a las TRES queries de mensajes: simula el mundo
    post-recorte, donde esos mensajes ya no existen.
    """
    p = {"account": cuenta, "msgs_desde": msgs_desde}
    cur.execute(con_piso_de_mensajes(_LAST_AGENT_SQL), p)
    last_agent = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(con_piso_de_mensajes(_PRIMARY_AGENT_SQL), p)
    primary_agent = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(con_piso_de_mensajes(_LAST_MSG_SQL), p)
    msg_times = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute(_CONVS_SQL, {"account": cuenta})
    rows = cur.fetchall()

    by_ticket: dict = defaultdict(list)
    for ticket_id, conv_id, created_at, resolved_at in rows:
        first_at, last_at = msg_times.get(conv_id, (None, None))
        by_ticket[ticket_id].append({
            "conversation_id": conv_id,
            "created_at": created_at,
            "resolved_at": resolved_at,
            "first_at": first_at,
            "last_at": last_at,
            "last_operator_body": last_agent.get(conv_id),
            "operator_id": primary_agent.get(conv_id),
        })
    return by_ticket


def _fecha(texto: str | None, fin_del_dia: bool = False) -> datetime | None:
    """YYYY-MM-DD -> datetime UTC. --hasta incluye el dia entero, no su medianoche."""
    if not texto:
        return None
    d = datetime.strptime(texto, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d + timedelta(days=1, microseconds=-1) if fin_del_dia else d


def _dur(td: timedelta) -> str:
    """Duracion legible: horas hasta 2 dias, dias despues. Los gaps viven en horas y los
    desvios de atribucion en meses; una sola unidad hace ilegible uno de los dos."""
    horas = td.total_seconds() / 3600
    return f"{horas:.1f}h" if abs(horas) < 48 else f"{horas / 24:.1f}d"


def _imprimir(cuenta: str, c, n_ejemplos: int, filtrado=None) -> float:
    """Imprime el reporte de una cuenta y devuelve el % de flips SUCIOS."""
    fronteras = c.fronteras
    r = resumen(fronteras)
    eps = c.episodios or 1

    print(f"\n{'=' * 78}\nCUENTA: {cuenta}\n{'=' * 78}")
    print(f"  tickets {c.tickets:>9,}   episodios {c.episodios:>9,}"
          f"   fronteras {r.fronteras:>9,}")
    if filtrado is not None and filtrado.tickets_excluidos:
        # Nada se descarta en silencio: los tickets a caballo del borde se informan.
        print(f"  fuera de la ventana: {filtrado.tickets_excluidos:,} tickets "
              f"({filtrado.episodios_excluidos:,} episodios) excluidos enteros")

    print("\nEFECTO TOTAL (assign_sessions completa: gap + span + cascadas)")
    delta = c.sesiones_nueva - c.sesiones_vieja
    print(f"  sesiones                    {c.sesiones_vieja:>9,} -> "
          f"{c.sesiones_nueva:>9,}  ({delta:+,})")
    print(f"  episodios con otro session_id      {c.cambio_session_id:>9,}  "
          f"({100.0 * c.cambio_session_id / eps:.2f}%)")
    print(f"  episodios cuya sesion se recompuso {c.sesion_recompuesta:>9,}  "
          f"({100.0 * c.sesion_recompuesta / eps:.2f}%)   <- los que se re-scorean")

    print(f"\nFLIPS DE FRONTERA (solo donde el termino gap decide; GAP={GAP})")
    print(f"  flips                        {r.flips:>9,}")
    print(f"    corta -> mergea            {r.corta_a_mergea:>9,}   "
          f"(la regla nueva UNE lo que la vieja cortaba)")
    print(f"    mergea -> corta            {r.mergea_a_corta:>9,}")

    print("\nLOS DOS BALDES")
    print(f"  LIMPIO  (ambos timestamps en su ventana)  {r.limpios:>9,}  "
          f"({100.0 - r.pct_sucios:.1f}%)")
    print(f"  SUCIO   (alguno fuera de ventana)         {r.sucios:>9,}  "
          f"({r.pct_sucios:.1f}%)")
    print(f"    last_at del previo POSTERIOR a su resolucion  {r.sucios_por_prev_futuro:>7,}"
          f"   <- infla merges")
    print(f"    first_at del siguiente ANTERIOR a su nacimiento {r.sucios_por_ep_pasado:>5,}")
    print(f"    los dos                                        {r.sucios_por_ambos:>6,}")

    sucios = [f for f in fronteras if es_flip_de_gap(f) and es_sucia(f)]
    magnitudes = [d.magnitud for f in sucios
                  for d in (f.desvio_prev, f.desvio_ep) if d is not None]
    p = percentiles(magnitudes)
    if p:
        print(f"  escala del desvio en los SUCIOS   p50 {_dur(p['p50'])}   "
              f"p90 {_dur(p['p90'])}   max {_dur(p['max'])}")

    # El caso que reintroduce over-merge: la nueva mergea, con silencio negativo, y ese
    # silencio negativo lo produce un last_at que se fue despues de la resolucion.
    over_merge = [f for f in sucios
                  if f.gap_viejo > GAP and f.gap_nuevo <= GAP
                  and f.gap_nuevo < timedelta(0)
                  and f.desvio_prev is not None and f.desvio_prev.direccion == DESPUES]
    print(f"\n  RADIO DE IMPACTO                 {r.pct_sucios_sobre_fronteras:>8.3f}%   "
          f"({r.sucios:,} fronteras sucias sobre {r.fronteras:,})")
    print(f"  OVER-MERGE POR ATRIBUCION        {len(over_merge):>9,}   "
          f"(mergea por silencio negativo que produjo un last_at mal atribuido)")

    if n_ejemplos:
        limpios = [f for f in fronteras if es_flip_de_gap(f) and not es_sucia(f)]
        for etiqueta, muestra in (("LIMPIOS", limpios), ("SUCIOS", sucios)):
            if not muestra:
                continue
            print(f"\n  ejemplos {etiqueta} (hasta {n_ejemplos}):")
            for f in muestra[:n_ejemplos]:
                det = []
                if f.desvio_prev:
                    det.append(f"prev.last_at {f.desvio_prev.direccion} "
                               f"{_dur(f.desvio_prev.magnitud)}")
                if f.desvio_ep:
                    det.append(f"ep.first_at {f.desvio_ep.direccion} "
                               f"{_dur(f.desvio_ep.magnitud)}")
                print(f"    ticket {f.ticket_id}  {f.prev_id} -> {f.ep_id}")
                print(f"      gap viejo {_dur(f.gap_viejo):>9}   "
                      f"gap nuevo {_dur(f.gap_nuevo):>9}"
                      + (f"   [{'; '.join(det)}]" if det else ""))
    return r


def _veredicto(resumenes: dict) -> None:
    print(f"\n{'=' * 78}\nVEREDICTO\n{'=' * 78}")
    print(f"  {'cuenta':<12} {'sucios':>8} {'fronteras':>11} {'radio':>8} {'s/flips':>9}")
    for cuenta, r in resumenes.items():
        print(f"  {cuenta:<12} {r.sucios:>8,} {r.fronteras:>11,} "
              f"{r.pct_sucios_sobre_fronteras:>7.3f}% {r.pct_sucios:>8.1f}%")
    peor = max((r.pct_sucios_sobre_fronteras for r in resumenes.values()), default=0.0)
    print()
    if peor < UMBRAL_RADIO_SANO:
        print(f"  SANO por radio de impacto (< {UMBRAL_RADIO_SANO}% de las fronteras).")
        print("  Se puede re-materializar: las fronteras que quedarian mal por atribucion")
        print("  son una fraccion despreciable del universo.")
    elif peor < UMBRAL_RADIO_BLOQUEA:
        print(f"  REVISABLE ({UMBRAL_RADIO_SANO}-{UMBRAL_RADIO_BLOQUEA}% de las fronteras).")
        print("  Mirar los ejemplos SUCIOS (--ejemplos 20) antes de re-materializar.")
    else:
        print(f"  BLOQUEA (>= {UMBRAL_RADIO_BLOQUEA}% de las fronteras). Re-materializar")
        print("  HORNEA el bug del ETL en las fronteras, y despues el scoring lo hornea en")
        print("  la linea base. El gap por inactividad tiene que esperar al ETL, igual que")
        print("  el span.")
    peor_calidad = max((r.pct_sucios for r in resumenes.values()), default=0.0)
    if peor_calidad >= UMBRAL_CALIDAD_POBRE:
        print(f"\n  NOTA DE CALIDAD: {peor_calidad:.1f}% de los flips los mueven datos mal")
        print("  atribuidos. El radio es chico, pero de lo que el fix CAMBIA una parte")
        print("  grande la decide dato podrido: revisar los ejemplos igual.")
    print("\n  Los umbrales son un criterio acordado, no una medicion. 'radio' = sucios")
    print("  sobre el TOTAL de fronteras (manda el go/no-go); 's/flips' = sucios sobre lo")
    print("  que el fix cambia (calidad del fix, ruidoso con pocos flips).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cuenta", action="append",
                    help="cuenta a analizar (repetible; por defecto SCORING_ACCOUNTS)")
    ap.add_argument("--ejemplos", type=int, default=0,
                    help="cuantas fronteras concretas mostrar de cada balde")
    ap.add_argument("--tolerancia-min", type=int, default=5,
                    help="margen en minutos del borde de la ventana (default 5)")
    ap.add_argument("--desde", metavar="YYYY-MM-DD",
                    help="solo tickets con TODOS sus episodios desde esta fecha")
    ap.add_argument("--hasta", metavar="YYYY-MM-DD",
                    help="solo tickets con TODOS sus episodios hasta esta fecha")
    ap.add_argument("--msgs-desde", metavar="YYYY-MM-DD",
                    help="piso de fecha sobre los MENSAJES: simula el mundo post-recorte "
                         "(este es el filtro que corresponde a la decision de scope)")
    ap.add_argument("--forzar-produccion", action="store_true",
                    help="saltear el guard del nombre de la base")
    args = ap.parse_args()
    tolerancia = timedelta(minutes=args.tolerancia_min)
    # created_at en la base es timestamptz: las fechas del CLI se comparan en UTC.
    desde = _fecha(args.desde)
    hasta = _fecha(args.hasta, fin_del_dia=True)
    msgs_desde = _fecha(args.msgs_desde)
    if desde and hasta and desde > hasta:
        ap.error("--desde es posterior a --hasta")

    cfg = load_config()
    cuentas = tuple(args.cuenta) if args.cuenta else cfg.scoring_accounts
    with psycopg.connect(cfg.database_url, connect_timeout=8) as conn:
        conn.read_only = True  # el script NO escribe; que lo garantice el servidor
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            bd = cur.fetchone()[0]
            print(f"base: {bd}  (read-only)   cuentas: {', '.join(cuentas)}")
            print(f"tolerancia de borde: {args.tolerancia_min} min"
                  + (f"   |   piso de mensajes: {args.msgs_desde}" if msgs_desde
                     else "   |   sin piso de mensajes"))
            if not any(p in bd.lower() for p in PISTAS_DE_COPIA) \
                    and not args.forzar_produccion:
                print(f"\nABORTADO: '{bd}' no parece una copia (se buscan "
                      f"{', '.join(PISTAS_DE_COPIA)} en el nombre).")
                print("Solo lee, pero escanea `messages` completa. Para correrlo igual:")
                print("  --forzar-produccion")
                return 2

            pcts = {}
            for cuenta in cuentas:
                by_ticket = _episodios_por_ticket(cur, cuenta, msgs_desde)
                if not by_ticket:
                    print(f"\nCUENTA {cuenta}: sin episodios con ticket_id, se saltea.")
                    continue
                f = filtrar_tickets_completos(by_ticket, desde, hasta)
                if not f.tickets:
                    print(f"\nCUENTA {cuenta}: ningun ticket entra entero en la ventana "
                          f"({f.tickets_excluidos:,} excluidos). Se saltea.")
                    continue
                pcts[cuenta] = _imprimir(cuenta, comparar_reglas(f.tickets, tolerancia),
                                         args.ejemplos, f)
    if pcts:
        _veredicto(pcts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
