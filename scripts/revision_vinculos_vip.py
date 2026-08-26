#!/usr/bin/env python3
"""config/jugadores_vip.json -> docs/revision-vinculos-vip.txt

La lista de vinculos que NO se pueden dar por seguros, para revisar a mano. Sale del mismo
archivo que alimenta la tabla, asi que se regenera con cada dump y no se desincroniza.

POR QUE EXISTE. El `username` es un dato del CASINO y no existe en el CRM, asi que el
vinculo se DEDUCE. `src/vip.clasificar_vinculo` separa lo que se puede afirmar de lo que
no; esto imprime lo segundo, con la evidencia a la vista, para que una persona decida.

    python scripts/revision_vinculos_vip.py [salida.txt]
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.vip import CONFIRMADO, DUDOSO, PROBABLE  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "config" / "jugadores_vip.json"
SALIDA = RAIZ / "docs" / "revision-vinculos-vip.txt"
# "Activo" = escribio esta semana. Es el corte que separa lo urgente de lo que puede
# esperar: un vinculo dudoso de alguien que no escribe hace un mes no alerta a nadie.
DIAS_ACTIVO = 7


def _activo(ultimo: str | None, hoy: datetime.date) -> bool:
    if not ultimo:
        return False
    return (hoy - datetime.date.fromisoformat(ultimo)).days <= DIAS_ACTIVO


def construir(doc: dict, hoy: datetime.date) -> str:
    grupos: dict[str, list] = {CONFIRMADO: [], PROBABLE: [], DUDOSO: []}
    for j in doc["jugadores"]:
        v = j["vinculo"]
        for c in v.get("contactos") or []:
            grupos.setdefault(c.get("verificacion") or DUDOSO, []).append((j, v, c))
    total = sum(len(g) for g in grupos.values())
    act = {k: sum(1 for j, v, _ in g if _activo(v.get("ultimo_mensaje"), hoy))
           for k, g in grupos.items()}
    L: list[str] = []
    w = L.append
    w("REVISION MANUAL DE VINCULOS VIP")
    w("=" * 78)
    w(f"Generado {hoy}  ·  fuente {doc.get('fuente', '?')}  ·  base {doc.get('origen_bd', '?')}")
    w("")
    w("QUE ES ESTO")
    w("  Cada jugador del reporte del casino se vinculo a un contacto del CRM. El")
    w("  `username` es un dato del CASINO y no existe en el CRM, asi que el vinculo se")
    w("  DEDUCE. Aca estan los que no se pueden dar por seguros.")
    w("")
    w("  SOLO LOS CONFIRMADOS ESTAN ENCENDIDOS. El resto vive en `vip_players` con")
    w("  `es_vip = false`: en stanby, no borrados. Verificar uno es encenderlo.")
    w("")
    w("QUE CUENTA COMO EVIDENCIA DURA (cada una es una IDENTIDAD, no una inferencia)")
    w("  · el username ES un telefono y coincide EXACTO con contacts.number")
    w("  · el username normalizado ES el nombre del contacto (evelynpalacios = Evelyn Palacios)")
    w("  · el contacto esta en la cola `Jugadores VIP` del CRM -- lo dice el CRM, no nosotros")
    w("  · un operador lo cargo en extraInfo bajo la clave `usuario`")
    w("  · 5+ menciones CON etiqueta (`Estimado X`) y NADIE compitiendo por ese username")
    w("")
    w("LO QUE NO CUENTA")
    w("  Una mencion suelta. Un agente nombra a muchos jugadores: asi `brysuye` habia")
    w("  quedado pegado a `Cristhian Oleas`, que lo menciono UNA vez contra las 177")
    w("  menciones del contacto correcto.")
    w("")
    w(f"RESULTADO SOBRE {total} VINCULOS")
    for k, etiqueta in ((CONFIRMADO, "ENCENDIDOS, no requieren nada"),
                        (PROBABLE, "apagados · evidencia buena, no concluyente"),
                        (DUDOSO, "apagados · evidencia debil")):
        w(f"  {k.upper():11} {len(grupos[k]):3}   ({act[k]} activos)   {etiqueta}")
    w("")
    w("POR DONDE EMPEZAR")
    w(f"  Por los {act[DUDOSO] + act[PROBABLE]} activos de las dos listas de abajo: son los unicos")
    w("  que escribieron esta semana. El resto no habla hace mas de siete dias.")
    w("")
    w("COMO REVISAR")
    w("  Buscar el nombre entre comillas en el tablero (el buscador matchea por ese campo),")
    w("  abrir la conversacion y ver si es el jugador. Para ENCENDER uno verificado:")
    w("      UPDATE vip_players SET es_vip = true WHERE username = '<username>';")
    w("")
    for tier, nota in ((DUDOSO, "evidencia debil -- revisar primero"),
                       (PROBABLE, "evidencia buena pero no concluyente")):
        filas = sorted(grupos[tier],
                       key=lambda x: (not _activo(x[1].get("ultimo_mensaje"), hoy),
                                      x[0].get("rank") or 999))
        w("")
        w("=" * 78)
        w(f"{tier.upper()}  ({len(filas)})   — {nota}")
        w("=" * 78)
        ultimo = None
        for j, v, c in filas:
            a = _activo(v.get("ultimo_mensaje"), hoy)
            if a != ultimo:
                w("")
                w(f"--- {'ACTIVOS (escribieron esta semana)' if a else 'sin actividad reciente'} ---")
                ultimo = a
            w("")
            w(f"  username : {j['username']}   (#{j.get('rank') or '?'} {j.get('agencia')}"
              f" · VIP por {j.get('motivo')})")
            w(f"  contacto : \"{c.get('nombre') or 'sin nombre'}\"   cuenta {c['account']}")
            w(f"  actividad: {v.get('mensajes', 0)} mensajes   ·   ultimo"
              f" {v.get('ultimo_mensaje') or 'nunca'}")
            w(f"  evidencia: {'; '.join(c.get('pruebas') or ['sin evidencia'])}")
            w("  DECISION : [ ] correcto   [ ] MAL VINCULADO   [ ] no se")
    w("")
    w("=" * 78)
    w("FIN")
    return "\n".join(L) + "\n"


def main() -> int:
    salida = Path(sys.argv[1]) if len(sys.argv) > 1 else SALIDA
    doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    texto = construir(doc, datetime.date.today())
    salida.write_text(texto, encoding="utf-8")
    print(f"{salida}: {len(texto.splitlines())} lineas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
