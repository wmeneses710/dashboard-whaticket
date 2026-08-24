"""Ejemplos few-shot para la clasificacion del MOTIVO.

POR QUE ESTA EN SU PROPIO MODULO Y NO EN UN STRING. El few-shot vivia como un string
suelto en src/prompts.py y quedo con TRES motivos sin ningun ejemplo: `soporte_cuenta`,
`registro` y `problema`. Nada fallaba — simplemente el modelo adivinaba en esos tres.

Una comparacion de tres modelos (qwen3.5:4b, ornith:9b, lfm2.5:8b) sobre las mismas 14
sesiones reales dio **38% de acuerdo en el motivo**, y los desacuerdos caian EXACTAMENTE
en los motivos sin ejemplo:

    sesion (motivo real)   qwen3.5        ornith         lfm2.5
    008e9509 (registro)    registro       info           info
    000170cc (soporte)     soporte_cuenta soporte_cuenta deposito
    0004c9ab (retiro)      retiro         retiro         problema

Como estructura de datos, `tests/test_fewshot.py` puede exigir la cobertura como
CONTRATO: si se agrega un motivo y falta su ejemplo, el test rompe.

DE DONDE SALEN LOS EJEMPLOS. Minados de sesiones REALES de la ventana limpia
(2026-07-01+, la unica con 0% de mala atribucion) y elegidos por ANCLAS DETERMINISTAS —
no por el motivo que el LLM ya habia asignado. Elegirlos por la salida del modelo le
enseñaria su propio sesgo. Los textos estan abreviados y anonimizados; el CRITERIO es el
del caso real.

DOS TRAMPAS QUE LOS EJEMPLOS NUEVOS ENSEÑAN, medidas en los datos:
  1. MEDIA DEL CLIENTE != DEPOSITO. En soporte de clave el cliente adjunta la captura
     del error de login. `lfm2.5:8b` leyo `deposito` por eso. El gate determinista de
     deposito solo dispara en 6,1% de `problema`, asi que "hay imagen" no alcanza.
  2. UNA DISPUTA SE PREGUNTA. 3 de 4 disputas de apuesta se clasifican `info` porque el
     cliente formula una PREGUNTA ("por que me la dieron perdida?"). La necesidad es
     reclamar un resultado, no informarse.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Ejemplo:
    """Un caso few-shot: transcript abreviado -> motivo + hechos, con el porque.

    `porque` NO va al modelo como regla: va entre parentesis debajo del JSON, que es
    como el modelo chico aprende el criterio (imita ejemplos mejor que obedece prosa).
    """
    motivo: str
    transcript: str
    hechos: dict
    porque: str
    # LOS CODIGOS E DEL MANUAL, y `()` cuando no hubo falla. Entro el 2026-08-24: el bloque
    # de ejemplos -- 5.263 caracteres, la parte mas concreta del prompt -- no mencionaba
    # `errores` NI UNA VEZ, y el modelo lo devolvia vacio en 435 de 435 filas. Benchado
    # contra el host real con gemma4:12b: 4 de 4 sesiones de 2 estrellas daban `[]`, y
    # tambien con el campo en el `required` del schema (un array vacio cumple). No era el
    # schema: era que se le estaba enseñando a no ponerlo. Ver src/catalogo_atc.ERRORES.
    errores: tuple[str, ...] = ()


def _h(atendio: bool, extra: bool = False, cortesia: bool = False,
       maltrato: bool = False, **resto) -> dict:
    """Los 4 hechos requeridos + los opcionales. Evita olvidarse alguno."""
    return {
        "atendio_el_motivo": atendio,
        "hizo_accion_extra": extra,
        "cortesia_destacada": cortesia,
        "hubo_maltrato_grave": maltrato,
        **resto,
    }


# Orden pensado: primero las trampas de frontera (los motivos que se confunden entre si),
# despues los casos de nota. Maximo 2 por motivo para no reintroducir el sesgo de clase.
EJEMPLOS_MOTIVO: tuple[Ejemplo, ...] = (
    # ---- DEPOSITO: la plantilla cumple el piso -------------------------------
    Ejemplo(
        motivo="deposito",
        transcript="CLIENTE: [media/sin texto] / CLIENTE: hola / "
                   "OPERADOR: enseguida te cargo / OPERADOR: Saldo cargado",
        hechos=_h(True),
        porque='la plantilla "Saldo cargado" YA cumple el piso -> atendio=true',
    ),
    Ejemplo(
        motivo="deposito",
        transcript="CLIENTE: [media/sin texto] / CLIENTE: Abono 10 a deuda / OPERADOR: ing",
        hechos=_h(True),
        porque='"Abono a deuda" + comprobante DEL CLIENTE es deposito; "ing" confirma',
    ),

    # ---- SOPORTE_CUENTA: la trampa de la media del cliente -------------------
    Ejemplo(
        motivo="soporte_cuenta",
        transcript="CLIENTE: me ayudan con la contrasena del usuario nathaly365 / "
                   "CLIENTE: [media/sin texto] / "
                   "OPERADOR: Listo estimado, ingrese con la siguiente contrasena: Sorti365",
        hechos=_h(True, claridad="claro", cliente_reinsistio=False),
        porque="el cliente adjunta media pero es la CAPTURA DEL ERROR DE LOGIN, no un "
               "comprobante: media del cliente NO implica deposito. Pide su clave -> "
               "soporte_cuenta",
    ),

    # ---- REGISTRO: quiere una cuenta nueva ------------------------------------
    # OJO: en ESTE negocio el alta NO se hace con un link. El OPERADOR pide los datos en
    # el chat y devuelve usuario y clave. El negocio lo marco como falso el 2026-08-06 (no
    # existe codigo de afiliado y el registro lo hace el operador), se saco el fragmento de
    # coaching que lo repetia — 151 de 624 recomendaciones, el 24,2% — pero este ejemplo
    # seguia ENSEÑANDO lo contrario, con premio explicito ("mando el link -> accion extra").
    # Los ejemplos le ganan a las instrucciones: por eso el modelo seguia recomendando un
    # link inexistente aunque src/prompts.py ya decia como es.
    Ejemplo(
        motivo="registro",
        transcript="CLIENTE: Hola / CLIENTE: Quiero crear una cuenta / "
                   "OPERADOR: Con gusto te la creo. Pasame nombre completo, correo y "
                   "numero / CLIENTE: Ana Rios ana@mail.com 0991234567 / "
                   "OPERADOR: Listo, tu usuario es anarios y la clave 12345, cambiala "
                   "en tu primer ingreso",
        hechos=_h(True, extra=True, claridad="claro", cliente_reinsistio=False),
        porque="pide crear cuenta -> registro (NO info, NO promo aunque el operador "
               "mencione beneficios). El alta se hace ACA: pidio los datos y entrego "
               "usuario y clave en el chat -> eso es cerrar el alta, no mandar un link",
    ),
    # El caso del ejemplo del negocio (2026-08-07): el operador OFRECIO crear la cuenta y
    # el cliente no volvio. Hizo lo que podia; lo que falta es explicar COMO sigue el
    # tramite, no un link. Sin este ejemplo el modelo listaba "no proporciono un link de
    # registro" como error, que es un reproche por algo que no existe.
    Ejemplo(
        motivo="registro",
        transcript="CLIENTE: Quiero registrarme y recibir mi Bono de $5 / "
                   "OPERADOR: Trabajo como agente de Sorti365 y por tu primera recarga "
                   "tengo una Freebet de $5 / "
                   "OPERADOR: Te creo un usuario para que juegues y te voy explicando? / "
                   "(el cliente no volvio a escribir)",
        hechos=_h(True, extra=True, claridad="dudoso", cliente_reinsistio=False),
        porque="OFRECIO crear la cuenta, que es el empuje concreto de este negocio -> "
               "atendio el motivo. El alta no se cerro porque el CLIENTE no contesto, no "
               "por el operador: NO es un error suyo. Lo mejorable es que no explico como "
               "sigue el tramite ni que datos necesita, y arrancar pidiendo datos "
               "personales sin aclarar para que son resta confianza -> claridad dudosa",
    ),

    # ---- PROBLEMA: la disputa se formula como pregunta -----------------------
    Ejemplo(
        motivo="problema",
        transcript="CLIENTE: hice esta apuesta pero me la registra como perdida y si "
                   "gano Inglaterra / CLIENTE: [media/sin texto] / "
                   "OPERADOR: la apuesta esta bien determinada, ese mercado significa "
                   "que Inglaterra debia marcar desde el momento de la apuesta",
        hechos=_h(True, claridad="claro", cliente_reinsistio=False),
        porque="ESTA REDACTADO COMO PREGUNTA pero la necesidad es RECLAMAR un resultado, "
               "no informarse -> problema, NO info. El operador explico la regla: el "
               "piso se cumple aunque el reclamo no proceda (no puede cambiar el "
               "resultado de una apuesta)",
    ),

    # ---- RETIRO: el comprobante lo manda el OPERADOR -------------------------
    Ejemplo(
        motivo="retiro",
        transcript="CLIENTE: Usuario carlos2311 Agencia Burkina Monto 50 Cedula 1351845092 "
                   "Banco Guayaquil / OPERADOR: Tu retiro esta en proceso, en breve te "
                   "enviamos el comprobante / OPERADOR: [media/sin texto]",
        hechos=_h(True, claridad="claro", cliente_reinsistio=False),
        porque="el cliente manda DATOS (usuario, agencia, monto, cuenta) y el "
               "COMPROBANTE lo manda el OPERADOR -> retiro. NO asumas fracaso por no "
               "ver la media",
    ),

    # ---- INFO: cliente sin necesidad y consulta puntual ----------------------
    Ejemplo(
        motivo="info",
        transcript="CLIENTE: Gracias / OPERADOR: Con gusto estimado, cualquier cosa avisas",
        hechos=_h(True),
        porque="el cliente no planteo consulta y el operador respondio cordial -> "
               "aceptable, NO deficiente",
    ),
    Ejemplo(
        motivo="info",
        transcript="CLIENTE: cual es el minimo de deposito? / "
                   "OPERADOR: El minimo es $5. Si queres te abro la cuenta ahora, "
                   "pasame nombre y correo",
        hechos=_h(True, extra=True, claridad="claro", cliente_reinsistio=False),
        porque="responde lo puntual ($5) + proximo paso CONCRETO y del mecanismo real "
               "(ofrece abrir la cuenta y pide los datos) -> claridad=claro",
    ),

    # ---- PROMO: no atendio, y la deflexion generica --------------------------
    Ejemplo(
        motivo="promo",
        transcript="CLIENTE: Como obtengo los bonos? / OPERADOR: Hola?",
        hechos=_h(False),
        errores=("E01",),
        porque="no atendio -> deficiente; pero NO hubo insulto -> maltrato=false, NO es "
               '"mala". E01: contesto sin leer lo que el cliente pregunto',
    ),
    Ejemplo(
        motivo="promo",
        transcript="CLIENTE: Como reclamo mis 10 giros? / "
                   "OPERADOR: es super facil, solo crea tu cuenta",
        hechos=_h(True, claridad="confuso", cliente_reinsistio=False),
        errores=("E03",),
        porque="NO explica COMO obtener los giros; deflexion generica que no responde lo "
               "puntual -> claridad=confuso. E03: la respuesta es ambigua",
    ),
)


def formatear_fewshot() -> str:
    """El bloque de ejemplos tal como entra al prompt del sistema.

    Formato: transcript en una linea, el JSON de hechos debajo (parseable por si solo,
    asi el modelo copia una forma valida) y el porque entre parentesis.
    """
    import json

    lineas = ["EJEMPLOS (aprende de estos HECHOS; no copies el texto, copia el CRITERIO):"]
    for i, e in enumerate(EJEMPLOS_MOTIVO, start=1):
        salida = {"motivo": e.motivo,
                  "dimensions": {"errores": list(e.errores)},
                  **e.hechos}
        lineas.append("")
        lineas.append(f"[{i}] {e.transcript}")
        lineas.append(f"-> {json.dumps(salida, ensure_ascii=False)}")
        lineas.append(f"({e.porque})")
    return "\n".join(lineas)
