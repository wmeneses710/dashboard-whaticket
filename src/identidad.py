"""QUIEN ATENDIO: la regla de identidad del operador, en un solo lugar.

Igual que el horario vive en `src/horario.py`, la identidad vive aca. No es un capricho de
orden: esta regla ya divergio dos veces.

EL 2026-08-12 se unificaron las 5 consultas de `queries.py` que tenian la expresion INLINE...
y quedaron TRES copias mas en `operators_status.py` (el modal de prender/apagar) y una en el
front. Consecuencia que reporto el negocio: seguia apareciendo "Operador sin identificar" en
el modal, que ademas NO conocia el split de `Operador borrado por Whaticket` y NO filtraba
`eval_status` -- asi que 4 filas SALTEADAS, sin un solo mensaje del negocio, creaban un
operador fantasma sobre el que alguien podia prender o apagar.
Y peor: `operators_status.py` tenia su propia copia del `translate` de acentos con el bug de
la ñ (23 caracteres contra 24) que ya se habia arreglado en `queries.py`.

DOS ETIQUETAS, NO UNA. Colapsar las dos causas en un solo cajon es peligroso justamente
porque la etiqueta se puede APAGAR: si un bug futuro rompe la atribucion de alguien ACTIVO,
su trabajo caeria en el mismo cajon apagado y desapareceria sin que nadie se entere.
MEDIDO sobre 130.558 filas evaluadas:
  - 128 tienen `user_id` pero NO hay fila en `users` -> el CRM BORRO al usuario. Causa
    conocida y cerrada (2 personas, ene/feb/may 2026) y el nombre NO es recuperable: 0 de sus
    745 mensajes trae la firma `*Nombre:*`. Esa se APAGA.
  - 675 no tienen NI `user_id` NI firma -> el fallo es NUESTRO. De esas, **640 tienen mensajes
    de un humano**: trabajo real sin nombre. Esa queda VISIBLE.
"""
from __future__ import annotations

BORRADO_POR_CRM = "Operador borrado por Whaticket"
SIN_IDENTIFICAR = "Operador sin identificar"

def expr_resuelto(*, nombre: str = "u.name", firma: str | None = "cs.user_name",
                  user_id: str = "cs.user_id", uid: str = "u.id") -> str:
    """El nombre RESUELTO, parametrizado por los ALIAS de quien pregunta.

    `users.name` manda y la firma `*Nombre:*` es el fallback (38 de 67 operadores de
    `sistemas` no existen en `users`: no estar en el catalogo es la NORMA ahi, no la
    excepcion). La causa del vacio se distingue por el JOIN: un `user_id` que no resuelve
    contra `users` es un usuario que el CRM BORRO.

    Los alias existen porque la misma regla se pregunta desde TRES universos distintos y NO
    se puede hardcodear uno solo:
      - `conversation_scores`: la firma vive en `cs.user_name`.
      - los cuadros de /api/charts: arrancan de `conversations` y la firma llega por el CTE
        `op_sig` (`sig.name`), resuelta en Python -- ahi no hay `cs` que preguntar.
      - `potential_clients`: NO hay firma (`firma=None`); solo el catalogo.

    Que los cuadros no tuvieran el split era un agujero, no un detalle: el apagado matchea
    POR NOMBRE, asi que apagar a un borrado desde el modal (que dice "borrado por Whaticket")
    no lo sacaba de los cuadros (que decian "sin identificar"). El mismo agujero que el
    comentario de `_OP_CHARTS` dice haber cerrado en agosto, reabierto por el split.
    """
    fuentes = f"coalesce({nombre}, {firma})" if firma else nombre
    return (f"coalesce(nullif({fuentes}, ''), "
            f"CASE WHEN {user_id} IS NOT NULL AND {uid} IS NULL THEN '{BORRADO_POR_CRM}' "
            f"ELSE '{SIN_IDENTIFICAR}' END)")


# El caso de siempre: una consulta sobre `conversation_scores` con LEFT JOIN a `users`.
OPERADOR_RESUELTO = expr_resuelto()

# HAY RASTRO DE UN OPERADOR. `agent_message_count > 0` es la cuarta puerta y no es cosmetica:
# el guard viejo (u.name / user_name / user_id) tiraba EN SILENCIO las sesiones donde un
# humano escribio y no lo pudimos nombrar. Que no sepamos quien fue no puede significar que el
# trabajo no exista. El solo-bot sigue afuera: ahi no hubo operador que evaluar.
HAY_OPERADOR = ("(u.name IS NOT NULL OR nullif(cs.user_name, '') IS NOT NULL "
                "OR cs.user_id IS NOT NULL OR cs.agent_message_count > 0)")


# PARA MOSTRAR una fila sola (detalle, tarjeta, lista de tickets): la etiqueta si hay rastro
# de un operador, NULL si no hubo ninguno. Sin el CASE, una conversacion 100% bot saldria
# rotulada "Operador sin identificar", que es peor que no decir nada: inventa una persona.
# El front solo pinta lo que llega y su "—" es para el NULL; no re-deriva la regla.
OPERADOR_O_NADA = f"CASE WHEN {HAY_OPERADOR} THEN {OPERADOR_RESUELTO} END"


def clave_sql(expr: str) -> str:
    """Clave de comparacion de nombres: minusculas y sin acentos.

    Las dos cadenas de `translate` TIENEN QUE MEDIR LO MISMO. Estaban en 23 contra 24 y
    Postgres no se queja -- ignora el sobrante y DESPLAZA el mapeo desde el caracter 16:
    `ñ` caia en 'a' en vez de 'n', asi que `Muñoz` NO matcheaba con `Munoz`, que es justamente
    lo que esta funcion existe para resolver. Latente hasta el 2026-08-12 (cero nombres con ñ
    en `users`, `user_name` y `operator_status`), pero le pegaba al primer Muñoz/Peña/Nuñez.
    Las mayusculas acentuadas se sacaron: el `lower()` va ANTES del `translate`, asi que nunca
    llegaban -- eran codigo muerto, y eran el origen del desalineo.
    """
    return (f"translate(lower({expr}), "
            "'áéíóúüàèìòùäëïöñ', 'aeiouuaeiouaeion')")
