#!/usr/bin/env python3
"""Banco de casos para auditar el PROMPT contra el modelo real. Repetible.

PARA QUE. Los tests de `tests/test_prompts.py` verifican el TEXTO del prompt; esto verifica
que el MODELO haga lo que el texto pide. Aisla el LLM del post-proceso: llama
`build_motivo_prompt` + `chat_json` y calcula la etiqueta con `label_from_facts`, sin las
PIEZAS deterministas del scorer. Asi se separa "el modelo juzga mal" de "la regla traduce mal".

POR QUE HACE FALTA. Auditoria del 2026-08-12 contra qwen3:14b: `cliente_reinsistio` -- el
hecho que DEMOTA a deficiente/mala -- reconocia el "?" literal y nada mas. El caso mas
explicito posible ("llevo 40 minutos esperando", "me estan ignorando?") daba false, y un
ghosteo de 3 mensajes del cliente salia `buena`. Un banco de casos lo agarra en 30 segundos;
leyendo notas de produccion no se ve nunca.

    python -m scripts.eval_prompt                    # todo el banco
    python -m scripts.eval_prompt --categoria escala
    python -m scripts.eval_prompt --repeticiones 3   # ademas mide estabilidad
Necesita OLLAMA_* en el entorno (usa .env / .env.local igual que el worker).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config                                    # noqa: E402
from src.llm import OllamaClient                                      # noqa: E402
from src.prompts import build_motivo_prompt, build_motivo_schema      # noqa: E402
from src.rubrics import label_from_facts                              # noqa: E402
from src.signals import (                                             # noqa: E402
    client_asked_question,
    client_reasked,
    operator_pushed,
    operator_resolved,
)


# RELOJ. Los casos que NO le importan al tiempo se sellan con +60 s entre mensajes; los que
# SI (los de riesgo del horario y de la espera legitima) pasan `seg` explicito. `_sellar` los
# completa al final, asi el banco viejo sigue funcionando sin tocar cada caso.
_BASE = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)


def _cli(t: str, media: str = "chat", seg: int | None = None) -> dict:
    return {"from_me": False, "is_note": False, "body": t, "media_type": media, "_seg": seg}


def _op(t: str, media: str = "chat", seg: int | None = None) -> dict:
    return {"from_me": True, "is_note": False, "body": t, "media_type": media,
            "sent_from": "OPERATOR", "_seg": seg}


def _cierre(quien: str = "Mario", seg: int | None = None) -> dict:
    return {"from_me": True, "is_note": True, "_seg": seg,
            "body": f"{quien} *resuelto* la conversación"}


def _sellar(msgs: list[dict]) -> list[dict]:
    """Pone `created_at` en cada mensaje: el `seg` explicito si lo trae, o +60 s del previo."""
    out, reloj = [], 0
    for m in msgs:
        seg = m.get("_seg")
        reloj = seg if seg is not None else reloj + 60
        m2 = {k: v for k, v in m.items() if k != "_seg"}
        m2["created_at"] = _BASE + timedelta(seconds=reloj)
        out.append(m2)
    return out


_DESPEDIDA = "Mucha suerte hoy, esperamos poder atenderte de nuevo pronto 🍀"
_CUENTA = "Te paso los datos del Banco Pichincha, cuenta 2100349661"

# Cada caso: (nombre, categoria, mensajes, motivo esperado o None, labels aceptables, kwargs).
# `labels` vacio = no se evalua la nota (el caso mira solo el motivo).
CASOS: list[tuple] = [
    # --- ESCALA: que use las 5 bandas -------------------------------------------------
    ("optimo con uplift", "escala", [
        _cli("Buenas, me ayudas con una recarga de 10?"),
        _op(f"Claro Carlos, {_CUENTA}"), _cli("", "image"),
        _op("¡Listo Carlos! Tu saldo ya está disponible 🍀"),
        _op("Con tu segunda recarga tenés un bono del 50%: se activa al depositar y lo "
            "liberás apostando el monto. Te dejo el link https://sorti365.com/promos"),
     ], "deposito", {"excelente"}, {"deposit_hint": True}),
    ("trabajo bien hecho, sin extra", "escala", [
        _cli("Buenas, me ayudas con una recarga de 10?"),
        _op(f"Claro, {_CUENTA}"), _cli("", "image"),
        _op("Gracias por tu recarga. Tu saldo ya está disponible 🍀"),
     ], "deposito", {"buena"}, {"deposit_hint": True}),
    ("dejo un hueco concreto", "escala", [
        _cli("Cuanto tarda un retiro de 50?"), _op("Se procesa el mismo día"),
        _cli("Y necesito verificar la cuenta primero?"), _op("Cualquier cosa me escribes"),
     ], None, {"aceptable", "deficiente"}, {}),
    ("no atendio lo que pedia", "escala", [
        _cli("Hola, no me deja entrar a mi cuenta, dice clave incorrecta"),
        _op("Buenas tardes 😉"), _op("Te invito a que pruebes nuestras promociones de hoy"),
     ], "soporte_cuenta", {"deficiente", "mala"}, {}),
    ("maltrato explicito", "escala", [
        _cli("Ya te mande el comprobante tres veces, que pasa?"),
        _op("Deja de molestar, no seas tan pesado, ya te dije que esperes"),
     ], None, {"mala"}, {}),

    # --- REINSISTENCIA: el hecho que DEMOTA (roto hasta el 2026-08-12) ----------------
    ("ghosteo: pide ayuda 2 veces", "reinsistencia", [
        _cli("Buenas, mande el comprobante hace una hora"), _cli("me ayudan?"),
        _cli("hola?"), _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("ghosteo: repite el pedido textual", "reinsistencia", [
        _cli("me acreditan la recarga de 20 por favor"),
        _cli("me acreditan la recarga de 20 por favor"),
        _cli("me acreditan la recarga de 20 por favor"), _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("ghosteo: reclama el silencio", "reinsistencia", [
        _cli("mande el comprobante de 20"),
        _cli("llevo 40 minutos esperando y nadie me contesta"),
        _cli("me estan ignorando?"), _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("ghosteo: un '?' suelto", "reinsistencia", [
        _cli("mande el comprobante"), _cli("?"), _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("el cliente se fue CONFORME (no es reinsistencia)", "reinsistencia", [
        _cli("me ayudas con la recarga?"), _op("Listo, ya está acreditado 🍀"),
        _cli("gracias!"), _cli("perfecto"),
     ], None, {"buena", "excelente"}, {}),

    # --- MOTIVO: la tabla de desambiguacion ------------------------------------------
    ("cliente manda comprobante -> deposito", "motivo", [
        _cli("Buenas me recarga"), _cli("", "image"), _op("ing"),
     ], "deposito", set(), {"deposit_hint": True}),
    ("operador manda comprobante -> retiro", "motivo", [
        _cli("Monto a retirar: 50 Nombres: Carlos Perez Cedula: 0912345678 Banco: Pichincha"),
        _op("Tu retiro está en proceso 🔄"), _op("", "image"),
     ], "retiro", set(), {}),
    ("'como activo mi cuenta' -> soporte, NO registro", "motivo", [
        _cli("Hola, como activo mi cuenta?"),
        _op("Con gusto, ya tienes cuenta creada? cual es tu usuario?"),
        _cli("si, es carlos99"), _op("Listo, ya quedó activa, prueba ingresar"),
     ], "soporte_cuenta", set(), {}),
    ("cuenta creada en la sesion -> registro aunque arranque por promo", "motivo", [
        _cli("Que promociones tienen?"), _op("Tenemos bono del 100% en la primera recarga"),
        _cli("dale, me interesa"), _op("Pasame tu correo y celular y te la creo"),
        _cli("carlos@gmail.com 0987654321"),
        _op("Listo: Usuario carlos99 Clave 12345"),
     ], "registro", set(), {}),
    ("interes en bono sin alta -> promo", "motivo", [
        _cli("Que promociones tienen hoy?"),
        _op("Bono del 100% en tu primera recarga, se activa al depositar"),
     ], "promo", set(), {}),
    ("'abono a deuda' + comprobante -> deposito", "motivo", [
        _cli("Abono 2 a deuda"), _cli("", "image"), _op("recibido, ya lo cargo"),
     ], "deposito", set(), {"deposit_hint": True}),
    ("pregunta por comisiones -> info", "motivo", [
        _cli("Cuanto cobran de comision por retiro?"), _op("No cobramos comisión 🍀"),
     ], "info", set(), {}),
    ("no se le acredito -> problema", "motivo", [
        _cli("Hice una recarga de 30 hace 2 horas y no me aparece el saldo"),
        _op("Déjame revisar con el área de pagos y te confirmo"),
     ], "problema", set(), {}),
    ("cambio de contrasena -> soporte_cuenta", "motivo", [
        _cli("Quiero cambiar mi contraseña, no la recuerdo"),
        _op("Te la reseteo, al ingresar cámbiala por una nueva"),
     ], "soporte_cuenta", set(), {}),

    # --- ROBUSTEZ: el mismo caso dicho de otra manera --------------------------------
    ("parafraseo A del ghosteo", "robustez", [
        _cli("mande el comprobante hace una hora"), _cli("me ayudan?"), _cli("hola?"),
        _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("parafraseo B del ghosteo (mismo hecho)", "robustez", [
        _cli("envié mi comprobante hace un rato"), _cli("alguien me puede ayudar?"),
        _cli("sigo esperando"), _op(_DESPEDIDA),
     ], None, {"deficiente", "mala"}, {}),
    ("ruido: 12 plantillas antes del caso", "robustez",
     [_op(_DESPEDIDA), _op("Recuerda que tenemos un numero alterno 593995195842")] * 6 + [
        _cli("Monto a retirar: 50 Nombres: Carlos Perez Cedula: 0912345678 Banco: Pichincha"),
        _op("Tu retiro está en proceso 🔄"), _op("", "image"),
     ], "retiro", set(), {}),

    # --- RIESGO DE DARLE TIEMPOS: esperas que el reloj NO distingue -------------------
    # Los tres reparos del negocio: el HORARIO, la espera propia del PROCESO, y que cada
    # proveedor tarda distinto. Si `con_tiempos` rompe estos, no se prende.
    ("fuera de horario: escribe 03:00, contestan 08:05", "riesgo", [
        _cli("Buenas, me pueden cargar 10?", seg=0),          # 10:00 -> se corre abajo
        _op("Buenos días, con gusto. Te paso la cuenta", seg=18300),
        _cli("", "image", seg=18600), _op("Listo, ya está acreditado 🍀", seg=18700),
     ], None, {"buena", "excelente", "aceptable"}, {}),
    ("espera del PROCESO: el banco tarda 25 min, avisando", "riesgo", [
        _cli("Monto a retirar: 50 Cedula: 0912345678 Banco: Pichincha", seg=0),
        _op("Recibido, lo mando a procesar. Suele tardar hasta 30 min", seg=90),
        _cli("dale gracias", seg=150),
        _op("Sigue en proceso, en cuanto salga te mando el comprobante", seg=900),
        _op("", "image", seg=1560), _op("Ahí está tu comprobante 🍀", seg=1570),
     ], "retiro", {"buena", "excelente"}, {}),
    ("demora REAL en horario: 6 h sin avisar", "riesgo", [
        _cli("Buenas, me cargan 10? ya mande el comprobante", seg=0),
        _cli("", "image", seg=30),
        _op("Listo, ya está acreditado", seg=21600),
     ], None, {"aceptable", "deficiente"}, {}),
    ("multi-interaccion: la 1ra se ghosteo, las otras bien", "riesgo", [
        _cli("mande el comprobante de 20", seg=0), _cli("me ayudan?", seg=300),
        _cierre(seg=600),
        _cli("Buenas, me cargan 15?", seg=90000), _op("Listo, acreditado 🍀", seg=90060),
        _cierre(seg=90120),
     ], None, {"deficiente", "mala", "aceptable"}, {}),

    # --- CORTESIA / SIN NECESIDAD ----------------------------------------------------
    ("jerga afectuosa NO es maltrato", "cortesia", [
        _cli("ñaño me cargas 5?"), _op("De una pana, ya te lo dejo cargado, suerte causa 🍀"),
     ], None, {"buena", "excelente"}, {}),
    ("cliente sin necesidad -> aceptable, no deficiente", "cortesia", [
        _cli("gracias"), _cli("buenas noches"), _op("Un gusto, buenas noches 🍀"),
     ], None, {"aceptable", "buena", "excelente"}, {}),
]


def _hechos(llm, schema, msgs, kwargs, con_tiempos: bool) -> dict:
    system, user = build_motivo_prompt(_sellar(msgs), "", con_tiempos=con_tiempos,
                                       **kwargs)
    raw = llm.chat_json(system, user, schema)
    cl = raw.get("claridad")
    h = {
        "motivo": raw.get("motivo"),
        "atendio": bool(raw.get("atendio_el_motivo")),
        "extra": bool(raw.get("hizo_accion_extra")),
        "cortesia": bool(raw.get("cortesia_destacada")),
        "maltrato": bool(raw.get("hubo_maltrato_grave")),
        "reinsistio": bool(raw.get("cliente_reinsistio")),
        "claridad": cl if cl in ("claro", "confuso", "dudoso") else "dudoso",
    }
    # LAS DOS COMPUERTAS DETERMINISTAS, calcadas de score_by_motivo. Sin ellas el arnes
    # miente: `cliente_reinsistio` del modelo NO baja la nota por si solo -- entra como
    # `confuso_corroborado`, que es lo que convierte un `aceptable` en `deficiente`. Y la
    # `friccion` es 100% determinista (`client_reasked and not operator_resolved`), no la
    # opinion del modelo. Modelarlas mal daba 2 falsos fallos en el banco.
    sellados = _sellar(msgs)
    resolved = operator_resolved(sellados)
    reasked = client_reasked(sellados)
    asked = client_asked_question(sellados)
    pushed = operator_pushed(sellados)
    h["friccion"] = reasked and not resolved
    h["corroborado"] = reasked or h["reinsistio"] or (asked and not resolved and not pushed)
    h["label"] = label_from_facts(
        atendio_motivo=h["atendio"], hizo_accion_extra=h["extra"],
        cortesia_destacada=h["cortesia"], hubo_maltrato_grave=h["maltrato"],
        claridad=h["claridad"], friccion=h["friccion"],
        confuso_corroborado=h["corroborado"])
    return h


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--categoria", default=None)
    ap.add_argument("--repeticiones", type=int, default=1)
    ap.add_argument("--con-tiempos", action="store_true",
                    help="EXPERIMENTO: transcript con hora, deltas y fronteras")
    args = ap.parse_args()

    cfg = load_config()
    llm = OllamaClient(cfg.ollama_url, cfg.ollama_model, token=cfg.ollama_token or None,
                       timeout=180.0)
    schema = build_motivo_schema()
    print(f"modelo: {cfg.ollama_model} @ {cfg.ollama_url}  "
          f"tiempos={'SI' if args.con_tiempos else 'no'}\n")

    casos = [c for c in CASOS if args.categoria in (None, c[1])]
    por_cat: dict[str, Counter] = {}
    inestables = []
    cat_actual = None

    for nombre, cat, msgs, motivo_esp, labels_esp, kwargs in casos:
        if cat != cat_actual:
            print(f"--- {cat.upper()}")
            cat_actual = cat
        vistos = set()
        h = None
        for _ in range(args.repeticiones):
            h = _hechos(llm, schema, msgs, kwargs, args.con_tiempos)
            vistos.add((h["motivo"], h["label"]))
        fallas = []
        if motivo_esp and h["motivo"] != motivo_esp:
            fallas.append(f"motivo={h['motivo']} (esperado {motivo_esp})")
        if labels_esp and h["label"] not in labels_esp:
            fallas.append(f"label={h['label']} (esperado {'|'.join(sorted(labels_esp))})")
        if len(vistos) > 1:
            inestables.append((nombre, vistos))
        c = por_cat.setdefault(cat, Counter())
        c["fallo" if fallas else "ok"] += 1
        marca = "**" if fallas else "OK"
        detalle = "  ".join(fallas) if fallas else (
            f"motivo={h['motivo']} label={h['label']}")
        print(f"  {marca} {nombre:46s} {detalle}")
        if fallas:
            print(f"       modelo: atendio={h['atendio']} extra={h['extra']} "
                  f"cortesia={h['cortesia']} reinsistio={h['reinsistio']} "
                  f"claridad={h['claridad']}")
            print(f"       gates : friccion={h['friccion']} corroborado={h['corroborado']}")

    print("\n" + "=" * 78)
    total_ok = sum(c["ok"] for c in por_cat.values())
    total = sum(sum(c.values()) for c in por_cat.values())
    for cat, c in por_cat.items():
        print(f"  {cat:16s} {c['ok']}/{c['ok'] + c['fallo']}")
    print(f"  {'TOTAL':16s} {total_ok}/{total}")
    if inestables:
        print(f"\n  INESTABLES en {args.repeticiones} corridas:")
        for nombre, vistos in inestables:
            print(f"    {nombre}: {sorted(vistos)}")
    print(f"\n  llamadas: fast={llm.calls['fast']} fallback={llm.calls['fallback']} "
          f"empty={llm.calls['empty']}")
    sys.exit(1 if total_ok < total else 0)


if __name__ == "__main__":
    main()
