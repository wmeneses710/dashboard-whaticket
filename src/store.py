"""Armado y persistencia de filas en conversation_scores (UPSERT idempotente).

`build_score_record` es logica pura (testeable sin DB): junta datos de la
conversacion + metricas + router + (si aplica) el resultado del LLM en el dict
de columnas. `upsert_score` lo escribe por conversation_id.

La tabla es derivada y separada de las del ETL: es seguro TRUNCARLA y
re-scorear. Ver db/scores_schema.sql.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from src.metrics import (
    MessageStats,
    first_response_seconds,
    resolution_seconds,
    was_unassigned,
)
from src.scorer import ScoreResult
from src.segments import segment_for_queue

# 2026.08-rubricas-v5 (2026-08-11). El bump es OBLIGATORIO cada vez que cambia como se
# calcula la nota: sin el, las filas viejas y las nuevas quedan indistinguibles y no hay
# forma de comparar ni de volver atras. Lo que cambio contra v4:
#   - el GATE del comprobante deja de ser ciego al comprobante sin texto del cliente
#     (2 puertas: vocabulario real + acuse del operador) -> 23% de las que caian al pase
#     con LLM pasan a la rubrica determinista;
#   - "en breve tendras tu saldo disponible" ya NO cuenta como acreditacion;
#   - el ABANDONO exige que el cliente haya LEIDO el pedido (`messages.ack`): el 42,1% de
#     los abandonos que se reportaban eran inventados;
#   - PIEZA 6: devolverle la pelota al cliente que ya pidio registrarse es 'deficiente';
#   - el coaching apunta a la RAMA que produjo la nota, no a la estrella;
#   - `_PASO_RE` de soporte ve el vocabulario real del operador (+34,7% del bucket de 2);
#   - un emoji suelto y el saludo del widget web ya no se leen como pedido;
#   - se RETIRO el cap de uplift de `promo` (la PIEZA 2) y todo su cableado.
# Medido sobre 1.020 sesiones con el modelo de produccion: el promedio va de 4,03 a 3,97,
# pero el 93,8% de las notas NO se mueve — el movimiento se concentra en `deposito` (-0,35),
# `registro` (-0,14) y `soporte_cuenta` (+0,14).
#
# 2026.08-rubricas-v6 (2026-08-12). Sale de la auditoria de 5 frentes sobre los datos del
# rescore v5. Lo que cambio contra v5:
#   - CERRAR-Y-ADJUNTAR ES UN SOLO GESTO: la interaccion absorbe los mensajes del operador
#     que llegan hasta 2 min despues de la nota `*resuelto*`. El flujo real del retiro es
#     cerrar y mandar el comprobante con una MEDIANA DE 1,1 SEGUNDOS de diferencia, y ese
#     comprobante caia en la interaccion siguiente -> "nunca envio el comprobante".
#     VALIDADO re-corriendo la rubrica real: 132 de 139 retiros en 2 estrellas SUBEN
#     (113 a 4, 19 a 3), y ninguna de las 136 imagenes recuperadas es un broadcast
#     (`campaign_id` nulo en todas);
#   - un `*resuelto*` que se `*reabierto*` en el acto, sin que nadie hablara en el medio, no
#     es una frontera: es el CRM rebotando (7.406 pares, mediana 58,5 s);
#   - la PROMESA del operador ya no se lee como un PEDIDO: "te enviaremos el comprobante"
#     matcheaba el patron de abandono, y era el 99,0% de los abandonos de `retiro` y el
#     90,7% de los de agilidad en 5 estrellas;
#   - "ya puedes disfrutar tu saldo" ACREDITA: eran 106 sesiones en 2 estrellas y estaban
#     concentradas en una sola operadora (41,2% de sus notas contra el 10-11% de sus pares);
#   - los TIEMPOS y el OPERADOR describen la interaccion JUZGADA y no la conversacion, en
#     `deposito` y `retiro`: la resolucion mostrada baja de 118,5 h a 6,2 min de mediana en
#     324 sesiones, y se corrigen 150 de las 152 notas que se le cargaban a un operador que
#     ni aparecia en la interaccion juzgada;
#   - `deposit_mismatch` reconcilia contra LAS DOS PUERTAS del gate: 840 de 889 filas que
#     marcaban discrepancia no tenian ninguna (quedan las 49 del camino con LLM, que es
#     donde el flag significa algo).
#
# 2026.08-rubricas-v7 (2026-08-12). Sale de auditar la corrida v6 y el PROMPT contra el
# modelo real. Lo que cambio contra v6:
#   - `registro` entra al VENTANEO POR INTERACCION, del que se habia quedado afuera: sobre la
#     sesion entera emparejaba los datos de un alta con las credenciales de OTRA, y el
#     `convirtio` que habilita el 5 agarraba una recarga de cualquier interaccion mientras el
#     texto afirmaba "en la misma conversacion". VALIDADO re-corriendo la rubrica real sobre
#     1.717 sesiones: 1.508 de UNA interaccion no se mueven y **27 cambian, TODAS hacia
#     abajo** (13 de 5->4, 6 de 5->3, 4 de 5->2, 4 de 3->2);
#   - el texto ya no dice "Creo la cuenta NUNCA despues de recibir los datos": cuando las
#     credenciales salen ANTES de los datos la espera NO se puede medir, y la frase no la
#     afirma. Eran 14 filas, LAS 14 con 5 estrellas, mas 43 con "tardo nunca";
#   - PROMPT, `cliente_reinsistio`: reconocia el "?" literal y nada mas -- el caso mas
#     explicito ("llevo 40 minutos esperando", "me estan ignorando?") daba false. Es el hecho
#     que DEMOTA, asi que roto empujaba las notas hacia ARRIBA. Medido contra qwen3:14b: de
#     1 de 4 formas reconocidas a 5 de 5;
#   - PROMPT, `atendio_el_motivo`: una DESPEDIDA ya no atiende. "Mucha suerte hoy" es cierre,
#     no atencion; "listo"/"ing"/"cargado" siguen contando porque ACUSAN el pedido. Cerro la
#     inestabilidad que hacia alternar el mismo ghosteo entre `buena` y `deficiente`;
#   - PROMPT, `deposito` vs `problema`: un reclamo por una recarga YA HECHA (en pasado, sin
#     adjuntar nada) es `problema`, no `deposito`;
#   - PROMPT: se reescribio como HECHO la unica regla que quedaba redactada en terminos de la
#     NOTA ("la nota es aceptable, NO deficiente"), instruccion muerta desde `label_from_facts`;
#   - `retiro` y `registro` reportan `deposit_observed=None` (= no observo) en vez de un
#     booleano: `deposit_mismatch` reconcilia el gate contra la observacion del LLM, y una
#     rubrica determinista no tiene opinion que reconciliar. Eran 28 de los 48 mismatches.
# Banco de casos del prompt (scripts/eval_prompt.py): 26/28, estable en 3 repeticiones.
# NO se cambio el transcript: se probo darle tiempos y fronteras al modelo y NO mejora
# (26/28 sin tiempos contra 25/28 con), asi que `format_transcript(con_tiempos=)` queda
# apagado. Ver el docstring de esa funcion.
#
# 2026.08-rubricas-v8 (2026-08-12). Sale de auditar los 1★ y 2★ de una copia fresca con v7
# corriendo: de 7 leidos en detalle, 2 estaban bien puestos y 5 no. Lo que cambio contra v7:
#   - VOCABULARIO DE ACREDITACION, tercera ronda y la mas grande. El patron se escribio
#     leyendo PLANTILLAS y el texto libre del operador se le escapa: "Tu saldo ya está en tu
#     cuenta", "Su saldo ya se encuentra en su cuenta", "ya lo tienes en tu cuenta",
#     "ya te lo cargué" (¡"cargo" se reconocia y "cargué" no!), "ya está realizado".
#     VALIDADO: **154 de 323 (47,7%)** depositos en 2★ "nunca confirmo" SUBEN — 132 a 4★,
#     19 a 3★, 3 a 5★. Con las dos rondas previas ("en breve" el 11-08 y "ya puedes disfrutar
#     tu saldo" el 12-08) el vocabulario ya explica ~360 sesiones mal calificadas;
#   - RAMA DEL RECHAZO en `deposito`: cuando la plata NO podia entrar por una razon valida
#     (titular incorrecto, boleta repetida, cuenta sin verificar), el trabajo del operador es
#     AVISARLO, y se califica por la velocidad de ese aviso — 4 si avisa en <=2 min, 3 si
#     tarda, 2 si nunca dice nada. **TECHO EN 4 a proposito**: el 5 significa "el mejor
#     escenario del motivo" y un deposito rechazado no lo es; el techo es honesto y mantiene
#     el incentivo de ayudarlo a arreglarlo. Con su coaching propio, porque el del 4 normal
#     habla del bono y el del 3 del acuse, y ninguno aplica. Dispara en 5 sesiones: quirurgico.
# NO se toco el corte en interacciones: se audito sobre 576 interacciones reales y esta bien
# (0 de mas de 24 h, 0 visitas pegadas, las largas son el cliente demorando dentro de un mismo
# pedido o el operador esperando antes de cerrar). Ver el docstring de src/interacciones.py.
#
# 2026.08-rubricas-v9 (2026-08-12). NO cambia ninguna nota: agrega un dato para poder
# AUDITARLAS. `dimensions.interaccion_juzgada_desde` guarda donde arranca la interaccion que
# la rubrica miro, y se bumpea porque `dimensions` es parte de la nota persistida.
#   - POR QUE no se deduce de la fila: cuando el ancla elige la PRIMERA interaccion,
#     `conversation_created_at` queda IDENTICO a cuando no hay ancla, y los dos casos piden
#     marcados opuestos en el chat (senalar una, o no senalar ninguna). MEDIDO sobre la copia:
#     de 31 sesiones multi-interaccion muestreadas, 28 caian en esa ambiguedad.
#   - PARA QUE: el modal mostraba la sesion entera como un chat corrido, y una sesion mergea
#     todos los episodios del ticket -- hay de 41, 33 y 20 interacciones. Quien auditaba leia
#     una nota de 2★ al lado de un tramo que habia salido bien y concluia que el sistema se
#     equivocaba. La nota describe UNA interaccion, no la sesion. Ahora el chat lo dice.
#   - Las filas de v8 y anteriores no lo traen: ahi no se senala ninguna, que es lo honesto.
#
# 2026.08-rubricas-v10 (2026-08-12). Sale de un caso de produccion que el negocio leyo con la
# corrida v9 ya andando: el cliente entrego nombre, celular y correo; el operador consulto a
# Atencion al Cliente, le dijo que YA TENIA CUENTA con otro agente, y lo derivo con el numero
# de ese agente. La rubrica le puso 2 estrellas -- "el alta quedo a medias" -- y la
# recomendacion decia "conviene decirle cuando la va a tener". Nunca la va a tener.
#   - RAMA DEL RECHAZO en `registro`, simetrica a la de `deposito` en v8. Cuando el alta NO
#     PODIA salir por una razon valida (el cliente ya tiene cuenta), el trabajo del operador es
#     AVISARLO y se califica por la velocidad de ese aviso: 4 si avisa dentro de los 5 min del
#     traspaso de datos (el umbral propio de `registro`), 3 si tarda, 2 si nunca dice nada.
#     TECHO EN 4 sin pedirlo: el 5 de `registro` es la conversion a deposito y se evalua fuera
#     de esa rama. Solo aplica SIN credenciales entregadas: "ya tienes cuenta creada, tu
#     usuario es X" es un alta EXITOSA (96 de los casos medidos). Con su coaching propio.
#     MEDIDO corriendo la rubrica sobre mensajes reales: de 184 registros con vocabulario de
#     rechazo, **74 estan hoy en 2 estrellas con un rechazo REAL y suben**; y 12 candidatos
#     eran FALSO POSITIVO ("este numero no esta registrado" es el operador PIDIENDO datos, lo
#     opuesto) -- de ahi el guard de negacion en el patron;
#   - DOS HUECOS DE PATRON en `redireccion`, del mismo caso. `re.IGNORECASE` no dobla acentos y
#     el patron estaba ASIMETRICO: la variante `-nos` tenia su forma acentuada y la `-me` no,
#     asi que "Escríbeme al <numero>" -- la plantilla de migracion Facebook -> WhatsApp, de las
#     mas frecuentes -- NO matcheaba. Y "a partir de ahora tu numero principal de ATENCION al
#     Cliente sera" tampoco, porque la alternancia pedia `atend`, que esta en "atenderemos" y
#     no en "Atencion". Los skips pasan de 3 a 9 sobre 3.000 sesiones de la copia.
# NO se toco el bucket B de `redireccion` (el traspaso como UN mensaje dentro de una
# conversacion real, 566 de 671 casos): ahi el motivo real sigue mandando. El caso que lo
# disparo cae justamente ahi, y lo que lo arregla es la rama del rechazo, no el skip.
#
# 2026.08-rubricas-v11 (2026-08-12). Sale de un caso de produccion: el unico mensaje del
# operador tras el comprobante fue "Ya le cargo mi amigo", y el analisis mostraba
# «✓ le confirmó que el saldo ya estaba acreditado». Eso es FALSO, y una tilde falsa es peor
# que una nota baja: le afirma al negocio una confirmacion que nunca existio.
#   - "YA LE CARGO" ES UNA PROMESA, no una acreditacion. `_strip_accents` corre ANTES del
#     match, asi que "cargó" (hecho) y "cargo" (yo lo cargo, ahora) quedaban identicos: el
#     acento era la unica señal y el codigo la borra. LO DECIDE EL PRONOMBRE, y lo confirma la
#     data -- vara: las 77.005 formas en pasado tienen una confirmacion posterior el 20% de las
#     veces. `ya LE/TE cargo` (1a persona, 441 msjs) la tiene el **59%**, y `ya SE cargo`
#     (= "se cargó", 47 msjs) el **11%**. O sea que los operadores MISMOS tratan la promesa y
#     la confirmacion como dos actos distintos. `se lo/la cargo` tambien es 1a persona; `se
#     cargo` a secas, no, y ESA sigue contando.
#     MEDIDO sobre las 210 sesiones de deposito que dicen la frase: 2★ pasa de 34 a 61 y 4★ de
#     143 a 118. Son 27 sesiones que bajan de "confirmó rápido" a "respondió pero nunca
#     confirmó" -- el mismo criterio que el negocio ya acepto el 2026-08-11 para "en breve
#     tendrás tu saldo" (103 sesiones). El caso que lo destapo se queda en 2★, pero con el
#     rationale correcto y sin la tilde falsa;
#   - `ahorita`/`ahora mismo` al guard de futuro: faltaba el futuro inmediato mas ecuatoriano;
#   - el INFINITIVO fuera de `acredit\w*`: "para acreditar necesito el comprobante" es el
#     operador PIDIENDO (183 mensajes) y "voy a acreditar" es intencion. Ninguno confirma nada.
#
# 2026.08-rubricas-v12 (2026-08-12). EL ANCLA ELIGE LA ULTIMA VISITA, en los tres motivos
# deterministas. Tomaba la PRIMERA -- el primer comprobante, el primer pedido, el primer
# traspaso de datos -- y una sesion mergea TODOS los episodios del ticket.
#   - MEDIDO sobre 1.180 sesiones con 2+ interacciones calificables: la primera y la ultima
#     estan separadas por una mediana de 8,6 h, un p90 de 285 h (12 dias) y un maximo de 266
#     dias. La nota describia la visita mas vieja y las demas se ignoraban (hay sesiones de
#     105 interacciones).
#   - LO QUE LO DECIDE es la ATRIBUCION, no la nota: **el 82% de esas sesiones tienen mas de
#     un operador** (hasta 10 distintos). Con la primera, la nota se le cargaba al que atendio
#     la visita vieja mientras el que atendio la ultima no aparecia. Cambiar de ancla mueve
#     600 notas de deposito y en 494 cambia TAMBIEN el operador responsable.
#   - EN LAS NOTAS EL CAMBIO ES NEUTRO, y conviene decirlo: deposito 3,34 -> 3,35 (383 bajan,
#     399 suben), retiro 3,44 -> 3,52, registro 2,68 -> 2,66. No se hace para aflojar la vara.
#   - NO SE PROMEDIA entre interacciones aunque castigue menos (2★ de 74 a 25 en las de 3+):
#     con 82% de sesiones multi-operador seria ponerle a una persona el trabajo de otra, y el
#     rationale dejaria de poder citar un tiempo verificable, que es como se audita.
#   - DOS PASOS, no uno: el ancla elige la INTERACCION (la ultima), y DENTRO de esa ventana el
#     reloj arranca en el PRIMER comprobante/pedido/traspaso de ESA visita. Si el cliente manda
#     tres imagenes seguidas, medir desde la ultima esconderia la demora.
#
# 2026.08-rubricas-v13 (2026-08-12). EL NOMBRE DEL OPERADOR VIVE EN LAS NOTAS DEL CRM, y es
# la QUINTA puerta de la identidad. El negocio seguia viendo "Operador sin identificar"; al
# abrir el objeto crudo aparecio que el dato estaba a la vista, en las notas internas que ya
# parseabamos para cortar interacciones:
#     *Asignado automáticamente* a Michelle
#     Michelle *resuelto* la conversación
# Usabamos la frontera y tirabamos el nombre. En esas conversaciones `conversations.user_id`,
# `tickets.user_id` y `messages.user_id` son TODOS NULL y no hay firma `*Nombre:*`.
#   - MEDIDO: de 127.898 sesiones con al menos un mensaje humano del negocio, 881 no tienen ni
#     user_id ni firma. La nota rescata **859 (98%)**, con 38 operadores distintos que hasta
#     hoy no existian en ningun cuadro.
#   - PRECISION validada contra la verdad conocida (las sesiones con UNA firma clara en el
#     cuerpo): la nota *resuelto* esta en 104.301 sesiones y el ultimo nombre acierta el 99%;
#     *aceptado* el 98% sobre 5.765; *asignado* el 98% sobre 95.893. El orden es cierre >
#     aceptacion > asignacion: de mas a menos evidencia de haber ATENDIDO.
#   - El nombre pasa por el MISMO guard `es_nombre_de_persona` que la firma. Sin el, "Gerente
#     de Cuentas" (28 sesiones) entraria como si fuera una persona.
#   - Se lee de la VENTANA JUZGADA, no de la sesion: una conversacion reabierta tiene varios
#     cierres y no son la misma persona (en el caso crudo, Michelle y Anya Alexandra).
#   - SEXTA Y ULTIMA PUERTA: la ASIGNACION del CRM (`conversations.user_id`, FK real a
#     `users`). Va ULTIMA y no primera aunque sea una FK: apunta a quien TIENE la conversacion
#     -- se transfiere -- y no a quien la trabajo. Medida contra la misma verdad conocida
#     acierta el **91%**, contra el 99% de la nota. Cierra el hueco exacto: de las 882 sesiones
#     sin user_id ni firma, la nota nombra 860 y la asignacion los 22 restantes. **882 de 882,
#     cero sesiones sin nombre.**
#   - NO se inventa un `user_id`: solo el nombre. La atribucion por entidad sigue necesitando
#     que el ETL la arregle.
# 2026.08-rubricas-v14 (2026-08-13). SALE DE UNA AUDITORIA DE CUATRO FRENTES sobre el rescore
# v13 en curso (18.545 filas), con la CAUSA RAIZ compartida por casi todo lo que aparecio: el
# FALL-THROUGH SIN ANCLA es el 43% de las filas evaluadas (7.531), no las 4.763 que se creia.
# `deposito`/`retiro`/`registro` quedan etiquetados con su motivo pero corren por el pase
# generico cuando su gate no encuentra la transaccion -- `registro` lo hace el 59,1% de las
# veces (2.464 de 4.168), `retiro` el 25,6% y `deposito` el 10,2%. El contraste que lo prueba:
# con ancla, 0 filas con resolucion mayor a un dia; sin ancla, 63 y un maximo de 78,7 dias.
# SEIS CAMBIOS, todos quirurgicos. Lo grande (ventana y atribucion del camino sin ancla) NO
# entra aca: es diseño y toca el pendiente de docs/handoff.md §10.
#
#   1. TECHO DEL FALL-THROUGH TRANSACCIONAL (`src/scorer.py`, PIEZA 7). El camino determinista
#      de `deposito` da 5 estrellas en 12 de 1.822 filas (0,7%); el fall-through, en 102 de 208
#      (49,5%) -- setenta veces mas, sin un comprobante que auditar. El techo es QUIRURGICO a
#      proposito: de esas 102, solo 23 AFIRMAN una acreditacion; las otras 79 son consultas
#      ("¿como recargo?") bien atendidas y conservan el 5. Un techo plano habria demotado a las
#      79 por un problema que no tienen. En `retiro` la media del operador ES la entrega y
#      protege el 5.
#   2. LA REINSISTENCIA DEL LLM DEMOTA (`src/scorer.py`). `friccion` se calculaba solo con
#      `client_reasked` (el reloj); el `cliente_reinsistio` que el modelo LEE no alimentaba
#      nada: 87 filas lo tenian en true con `friccion=false` y **71 (81,6%) quedaron en 4 y 5
#      estrellas**. Una de 5 se desmentia sola: "no ofrecio una solucion alternativa ni escalo
#      el caso cuando el cliente insistio en que ya llevaba 10 minutos esperando". La
#      proteccion determinista (`not resolved`) NO se toca.
#   3. EL 5 NO LLEVA CONSEJO CORRECTIVO, TAMPOCO EN EL CAMINO LLM (`src/scorer.py`). Eran 623
#      de 4.782 filas en 5 estrellas (13,0%), el 100% de las del pase con LLM (439/439). Los
#      fragmentos deterministas SI se conservan: el aviso de cambiar la contraseña no es un
#      reproche al operador, es una instruccion para el cliente.
#   4. EL CONSEJO DE `registro` 4 DEJA DE REPETIR EL REPROCHE (`src/registro.py`). 1.040 de
#      1.054 filas (98,7%) tenian "acompañarlo hasta la primera recarga" en el rationale Y en
#      la recomendacion; en el camino LLM pasaba 0 de 1.541 veces. Ahora dice COMO.
#   5. EL FORMULARIO BANCARIO NO SECUESTRA EL ANCLA DE `registro` (`src/registro.py`).
#      `_CEDULA_RE` es `\b\d{10}\b` y en Ecuador eso tambien es el numero de cuenta: un pedido
#      de retiro POSTERIOR y ajeno se volvia el ancla y el alta cerrada quedaba invisible (caso
#      `bcfc1510`: 2 estrellas y "el alta quedó a medias", falso). 14 sesiones con nota falsa,
#      pero la exposicion era enorme: de 70.559 mensajes del cliente con 10 digitos, 55.890
#      (79,2%) traen vocabulario bancario. El EMAIL sigue ganando siempre.
#   6. CUARTO HUECO DEL VOCABULARIO DE ACREDITACION (`src/signals.py`). Los tres anteriores
#      fueron de LEXICO; este es de SINTAXIS: ESTAR/SER + posesivo + recarga/saldo. 33 sesiones
#      en 2 estrellas de 8 operadores que SI confirmaron, **concentradas en personas**: Mel 17
#      (usa una PLANTILLA fija, asi que caia en cada deposito que atendia) y Romina 7. Validado
#      sobre las 171 sesiones reales de la muestra: el vocabulario pasa de ver 28 a ver 58.
#
# LOS DOS AGUJEROS DE LOS TESTS que dejaban pasar 3 y 4 con los 13 invariantes en verde:
# `test_el_cinco_no_lleva_consejo` llamaba a `score_deposito` DIRECTO y nunca al camino real de
# produccion; y `test_el_coaching_dice_COMO_no_solo_QUE_paso` aceptaba la palabra suelta
# "acompañ" como prueba de que el texto decia COMO -- y esa palabra era la del REPROCHE. Se
# cerraron los dos, mas un invariante nuevo que compara los dos campos de la MISMA fila con
# n-gramas de 5 palabras (4 es el tema, 5 seguidas es la misma oracion dos veces).
# 2026.08-rubricas-v15 (2026-08-13). EL RELOJ NO COBRA LA COLA. Las rubricas median desde el
# mensaje del CLIENTE hasta la primera respuesta del operador, sin saber cuando el CRM le
# ENTREGO la conversacion: todo lo que el ticket pasaba sin asignar se le cobraba a quien lo
# levantaba. `rg "asignad|assign" src/deposito.py src/info.py` no devolvia NADA.
#   - MEDIDO sobre las 6 filas de `deposito` en 2 estrellas por "tardo en avisarle": en 5 de 6
#     el reloj era casi todo COLA.
#         sesion     operador          reloj    cola    reaccion propia
#         48c251a2   Anya Alexandra    308,7    300,2         8,5
#         c324708f   Anya Alexandra    269,7    266,7         3,0
#         cc996f57   Anya Alexandra    110,3    108,9         1,4
#         347ffeac   Anya Alexandra     65,3     61,1         4,1
#         13f5f9da   Maria Jose         33,8     33,4         0,4
#     CUATRO DE LAS CINCO SON DE LA MISMA PERSONA: contesto entre 1,4 y 8,5 minutos y cobro 2
#     estrellas por "tardar". Con el umbral en 5 minutos, la cola sola ya se los comia.
#   - El mismo artefacto se confirmo en `info` (caso `7a08654d`: "respondio recien 11,3
#     minutos despues" cuando la operadora contesto en 44 SEGUNDOS).
#   - `src/operators.asignacion_at` lee `*Asignado automáticamente*` y `*aceptado*` (nunca
#     `*resuelto*`, que es el final) e `inicio_del_reloj` devuelve el mas TARDIO entre el
#     pedido y la entrega. Es la misma idea que ya rige en `espera_efectiva`, que descuenta el
#     horario: no se cobra lo que el operador no controla. Y el eje ya estaba medido desde el
#     2026-08-06 ("primer mensaje tras la asignacion sirve como eje, deposito 0,7 min de
#     mediana"); simplemente no se habia usado.
#   - SIN NOTA DE ENTREGA NO SE DESCUENTA NADA, y una entrega ANTERIOR al pedido tampoco: ahi
#     el operador ya tenia la conversacion y la demora es entera suya. Los dos con test.
#   - `soporte` queda AFUERA a proposito: califica con la MEDIANA de los turnos, no con un
#     reloj unico, y la cola solo afecta al primero. Descontarla ahi seria inventar.
#   - IMPACTO medido sobre las 199 filas deterministas de la copia: 180 sin cambio (90,5%),
#     **17 suben** (info 2->4 seis veces, info 3->4 tres, deposito 2->4 dos, deposito 2->3 dos,
#     info 2->3, deposito 3->4, retiro 3->4). Entre las que suben estan tres de las cuatro de
#     Anya que motivaron el cambio.
#
# EL CONSEJO DEL 4 PIDE TAMBIEN LA ESPERA. `operator_asked_and_waited` exige DOS cosas para
# dar el 5 -- preguntar "¿algo mas?" Y dejar una ventana antes de cerrar (que el cliente
# conteste, o 5 minutos) -- y el coaching solo hablaba de la primera.
#   - MEDIDO sobre 273 sesiones con el gate en False: 134 (49%) no dicen nada parecido, **58
#     (21%) SI preguntaron y los rechazo la ESPERA**, y 81 (30%) dicen una DESPEDIDA
#     ("escribeme cuando quieras"), que empuja al futuro en vez de retener la conversacion
#     abierta -- no es el mismo acto y el gate hace bien en rechazarla.
#   - Cumplimiento global: **8 de 122 (6,6%)**, y **78 de 122 (64%) estan en 4 estrellas
#     EXCLUSIVAMENTE por esto**. En `info` no lo cumple nadie (0 de 21).
#   - Los cuatro textos del 4 (`deposito`, `retiro`, `info`, `soporte`) conservan la razon
#     propia de su motivo y suman los 5 minutos. Dos tests lo atan.
#
# Y CUATRO ARREGLOS MAS, todos de la misma auditoria y todos con el mismo patron: una señal
# que decia que si cuando la respuesta era que no.
#   3. EL ANCLA DE `deposito` TIENE QUE SER UN COMPROBANTE, no la ultima imagen que paso.
#      `es_transaccion` exige contexto de recarga sobre la SESION y la eleccion es de
#      INTERACCION, asi que cualquier imagen posterior quedaba habilitada como ancla. Tres
#      casos reales, los tres con 2 estrellas y "nunca le confirmo": una imagen con caption
#      vacio sobre un problema de login (`0a61513b`), una foto de una finca entre 100
#      candidatas (`23ff3128`), y una pregunta de apuestas seis dias despues de dos recargas
#      confirmadas por OTROS operadores (`1f53cdc6`). Ahora se corrobora cada imagen en SU
#      interaccion con las dos puertas que la rubrica ya usa.
#   4. LA EXENCION POR ABANDONO SE ACOTA A QUIEN SE OFRECIO. Desactivaba el techo ENTERO de
#      `registro`: **45 filas del camino LLM con `cliente_abandono=true` llegaron a 5 estrellas,
#      contra 0 de las 2.061 con abandono=false**. La decision del 2026-08-07 protege al
#      operador que "ofrecio crear la cuenta y se quedo esperando -- hizo lo que podia", y ESO
#      SE CONSERVA intacto (su test sigue verde); lo que sale es el que solo recito la
#      plantilla de venta. Medido con el regex ya corregido: 37 de 45 ofrecieron de verdad y
#      conservan su nota, 8 no ofrecieron nada.
#   5. `_AL_PUNTO_RE` NO PUEDE LEER "TE REGISTRAS" COMO UNA OFERTA. El grupo de la ayuda era
#      OPCIONAL y el patron colapsaba a `te registr` a secas, matcheando lo que hace el
#      CLIENTE. Caso `9a83a433`: "...TE REGISTRAS, verificas tu cuenta y con tu primera carga
#      accedes a una freebet..." se leia como "fue al punto" mientras el rationale del LLM
#      decia -- con razon -- "no guio al cliente paso a paso ni le pidio los datos". Este
#      falso positivo era el que inflaba el punto 4 (daba 41 de 45 en vez de 37).
#   6. EL NUMERO SUELTO QUE CONTESTA UN PEDIDO BANCARIO. El arreglo de v14 miraba el
#      vocabulario del banco DENTRO del mensaje del cliente, y dejaba afuera la variante mas
#      comun: el operador pide los datos y el cliente escribe solo el numero. Caso `fda5a4f9`:
#      credenciales entregadas el 28-jul, y doce dias despues un "2101059380" que contestaba
#      "pasame nombre completos de titular, numero de cuenta, banco, cedula" se volvia el
#      ancla -> 2 estrellas y "el alta quedo a medias", falso. Ahora se mira el mensaje
#      ANTERIOR del operador.
#   7. ENTREGAR CREDENCIALES ES RESOLVER. `operator_resolved` nunca consultaba
#      `operator_sent_credentials`, que vive en el mismo modulo: un alta CERRADA se salteaba
#      como `sin_motivo`/`customer_media_only` cuando el cliente solo decia "gracias" o mandaba
#      un audio. 2 de 48 sesiones salteadas de la copia fresca.
# 2026.08-rubricas-v16 (2026-08-14). SALE DE UNA AUDITORIA DE CINCO FRENTES sobre el rescore
# v15 (comportamiento / calificacion / motivo / contexto / recomendacion), verificada despues
# hallazgo por hallazgo contra los datos. El hilo que une casi todo: TEXTOS Y SEÑALES QUE
# AFIRMABAN COSAS QUE LOS DATOS DESMENTIAN.
#
#   1. EL RATIONALE NO PUEDE DESMENTIR A UNA SEÑAL DURA. De las 2.311 filas de `registro` por
#      el camino LLM, 283 traen un rationale que afirma que no se pidieron los datos, y
#      corriendo `fue_al_punto` sobre los mensajes reales **149 (52,7%) lo afirman EN FALSO**
#      (134 'buena', 10 'aceptable', 5 'deficiente'). El operador lee una acusacion falsa
#      pegada a una nota que dice que hizo bien el trabajo.
#      NO SE USA `_CONTRADICE_RE`, que ya existia y era la solucion "obvia": ese patron es
#      para la nota de evidencia POR DIMENSION, y sobre el rationale general matchea el
#      **78,1% de los 'buena'** y el **92,3% de los 'deficiente'** -- habria borrado el texto
#      de casi todo el padron, incluidas las 134 afirmaciones CIERTAS. El guard mira la señal
#      (`registro.rationale_desmiente_el_pedido`), conserva el texto del modelo entero y le
#      anexa la correccion.
#   2. `cliente_reinsistio` SE RETIRA DE LA NOTA Y DEL TABLERO. Se creo para detectar al
#      operador que despacha con una PLANTILLA y no contesta el motivo -- ANTES de que
#      existieran los motivos, cuando no se sabia que queria el cliente. Analizadas las 103
#      filas donde dispara (categorias NO excluyentes): **39% son RAFAGAS** (mediana entre
#      mensajes < 60 s: como escribe la gente), **11% DUPLICADOS literales**, **7% el cliente
#      escribio UNA sola vez** (imposible reinsistir) y solo **14% es el caso buscado**.
#      Y NO SE ARREGLA CON UN PROMPT MEJOR: el fenomeno real es el **0,3%** del padron (7 de
#      2.760 con un detector determinista de plantillas) y el piso de ruido del modelo en
#      `registro` es **9,3%**. La inestabilidad del instrumento es mas de un orden de magnitud
#      mayor que la cosa a medir. Tampoco sirve el detector determinista: hay 212 plantillas
#      globales contra **1.675 propias de un operador** en 55 operadores, y una plantilla
#      recien creada tiene cero usos.
#      LO QUE OCUPA SU LUGAR: el eje de CLARIDAD. "La respuesta no contesto" es
#      `claridad='confuso'` corroborado por `(asked and not resolved and not pushed)`, que
#      nunca dependio de esta señal. Se sigue PERSISTIENDO el dato crudo para poder re-medirlo.
#      Impacto: de las 57 filas con friccion en el camino LLM, 21 tienen silencio DURO y la
#      conservan; 36 dejan de estar demotadas por la sola palabra del modelo.
#   3. LA CORTESIA NO COMPRA EL 5. `label_from_facts` subia a 'excelente' con
#      `hizo_accion_extra` **o** `cortesia_destacada`, y la cortesia es casi gratis: hay 212
#      plantillas calidas con mas de 300 usos, la mas repetida con 79.447. **ES EL MISMO BUG
#      QUE ROMPIO LA ESCALA VIEJA**, documentado en el docstring de src/deposito.py ("el 47,5%
#      de los depositos llegaba a 5 SOLO por cortesia"): las rubricas deterministas se
#      rehicieron para arreglarlo y el camino LLM quedo con la regla vieja.
#      MEDIDO: de 284 'excelente' del camino LLM, **130 (46%) no tienen el acierto
#      `iniciativa`** -- registro 30, deposito 40, problema 36, retiro 24. Bajan a 4. La
#      cortesia SIGUE siendo un acierto visible en `aciertos[]`: se reconoce, no se premia con
#      la nota maxima. Reparto del golpe: maximo -0,025 en el promedio de un operador.
#   4. EL RELOJ DE `registro` Y `promo` TAMPOCO COBRA LA COLA. v15 aplico `inicio_del_reloj` a
#      deposito/retiro/info y dejo estos dos afuera SIN que ninguna decision lo registrara (a
#      diferencia de `soporte`, cuya exclusion SI esta documentada arriba). Corrida pareada
#      sobre 4.297 filas deterministas: **promo 60 mejoran (2,2%), registro 2, CERO empeoran**.
#      El 2,2% de promo es proporcionalmente MAYOR que el 1,1% que el arreglo corrigio en
#      `deposito`: se le habia arreglado el reloj al motivo que menos lo sufria.
#      Y UNA ENTREGA POSTERIOR A LA ENTREGA NO PROBO NINGUNA COLA: si la nota del CRM llega
#      DESPUES de las credenciales, no se descuenta nada. Sin ese guard, 6 de las 8 filas que
#      mejoraban en `registro` decian "Creó la cuenta 0 segundos después de recibir los datos".
#   5. EL CLIENTE DICIENDO QUE SE RESOLVIO ES EL PISO QUE LE FALTABA A `problema`. Caso
#      `060725b4`, **1 estrella**: el operador contesta en 0,2 y 0,4 minutos y el cliente cierra
#      con "Si ya me salió. Todo bien. Muy amable." Tres cosas juntas: `operator_resolved` da
#      False porque el operador resolvio mandando un LINK (la señal solo mira confirmacion,
#      media o credenciales); `problema` es el UNICO motivo sin rubrica determinista ni piso;
#      y con `atendio=False` mas friccion la etiqueta cae a 'mala'.
#      `signals.cliente_confirmo_resuelto` es ground truth del unico que sabe si su problema se
#      arreglo. Patron DELIBERADAMENTE conservador, como MALTRATO_PATTERN: exige un verbo de
#      RESOLUCION, no cortesia. "ya está listo" NO entra -- caso `d594567c`, donde el cliente lo
#      dice y en el mensaje SIGUIENTE aclara "Estoy esperando su verificación no mas".
#   6. `worker.py::score_and_store` referenciaba `sess`, que no existe en su scope: NameError
#      latente en la cuarta puerta de atribucion. La ruta del contenedor usa
#      `score_session_and_store` (que si define `sess`); la expuesta es scripts/run_scoring.py.
#   7. LA FILA DICE CUANTA GENTE LA TRABAJO. El tablero valida la interaccion OPERADOR->CLIENTE
#      y cada interaccion se le asigna a alguien, pero la fila es UNA nota con UN operador.
#      MEDIDO separando las tres poblaciones que la auditoria mezclaba:
#          83,2% (12.948) una sola interaccion       -> atribucion honesta
#          16,8% ( 2.614) multi-interaccion
#                  2.110  ...con UN SOLO operador    -> atribucion honesta igual
#                    504  ...con VARIOS operadores   -> 3,2%, el caso a marcar
#      En esas 504 hay 2.734 interacciones y **1.824 (66,7%) son de alguien que NO recibio la
#      nota**; llegan a 10 operadores en una fila. NO SE MUEVE LA VENTANA: cualquiera que se
#      elija deja ese 66,7% afuera del que cobra. Se MARCA
#      (`dimensions.operadores_en_la_sesion` + chip), igual que `interaccion_juzgada_desde`.
#
# DOS CAMBIOS SE PROBARON, SE MIDIERON Y SE REVIRTIERON (el porque vive en el codigo):
#   - EL GUARD DE DEPOSITO EMBEBIDO (extenderlo a promo/info/soporte_cuenta). 67 filas, 33
#     bajaban. Al leer los transcripts, **2 de 2 casos muestreados entre los saltos de 5->2
#     eran FALSOS POSITIVOS**: consultas donde la recarga se HABLA y no ocurre ("De aquí a
#     mañana recargo"). `deposit_candidate_count` se dispara de mas cuando la recarga es el
#     TEMA. Subir la vara a `operator_acreditacion` no los elimino.
#   - RECORTAR LA VENTANA de las filas sin ancla. 2.000 filas cambiaban, con la resolucion
#     encogiendo 16,8x en la mediana. Pero **1.577 son agilidad**, y esa rubrica AGREGA la
#     sesion entera por diseño ("La espera más larga fue de 8,1 minutos, SOBRE 17 PEDIDOS").
#     Habria cambiado una inconsistencia por otra.
# 2026.08-rubricas-v17 (2026-08-17). SALE DE LA AUDITORIA DE LA CORRIDA v16 COMPLETA (71.111
# filas, 65.599 con nota, del 14/08 19:58 al 17/08 16:21). Los tres arreglos son de la misma
# familia y la peor que tiene este sistema: LA FILA ACUSA DE ALGO QUE SU TRANSCRIPT DESMIENTE.
# Ninguno cambia una vara: los tres corrigen una MEDICION.
#
#   1. EL PATRON DE CREDENCIALES NO CONOCIA EL USTED. `CREDENTIALS_PATTERN` aceptaba la frase
#      de entrega solo en informal ("tu usuario es X"), y las agencias escriben "SU usuario es
#      X y la contraseña tambien X". MEDIDO: de las 501 filas de `registro` en 2 estrellas con
#      el rationale "El cliente entregó sus datos pero nunca recibió su usuario y clave",
#      **201 (40,1%) tenian la entrega TEXTUAL en el transcript** (`a7c79fda`, `339fb197`,
#      `7d92c3a4`). Recalculadas las 4.616 filas deterministas de `registro`: **203 suben
#      (104 a 3★, 80 a 4★, 17 a 5★) y CERO bajan**; las 201 falsas quedaron en 0.
#      EL RADIO NO ERA SOLO LA NOTA. La señal alimenta `registro.se_creo_la_cuenta`, que FUERZA
#      el motivo a `registro` cuando el alta se cerro: **35 filas de jugador cambian de motivo**
#      (17 desde `promo`, 16 de ellas con 5 estrellas; 15 desde `soporte_cuenta`). Es la fuga
#      que este archivo ya documentaba mas arriba ("un alta CERRADA se salteaba") y que seguia
#      abierta por una palabra. Tambien entra en `operator_resolved` (piso del camino LLM) y en
#      la recomendacion de seguridad de cambiar la contraseña, que ahora si alcanza a esas
#      sesiones.
#   2. `soporte_cuenta` NO RECONOCIA SUS PROPIOS PASOS, Y NO ESCUCHABA AL CLIENTE. La rama "no
#      hubo intento" emite el texto mas duro de la rubrica ("ni un paso a seguir ni la certeza
#      de que su caso se escaló") y le faltaba el vocabulario mas usado del motivo: `intent[ae]`
#      ("intente nuevamente y me avisa"), `captura`/`pantallazo` ("me envia una captura de lo
#      que le sale") y `comunicar(te|se) a` ("Debes comunicarte a este número"). El ultimo va
#      con PREPOSICION porque `comunic` suelto ya se probo y matcheaba la plantilla de cierre.
#      Y `cliente_confirmo_resuelto` -- el cliente diciendo "Ya pude gracias" -- vivia SOLO en
#      el camino LLM, que esta rubrica no recorre: ahora tambien cuenta como intento, porque es
#      la evidencia mas dura que existe de que algo se hizo.
#      Recalculadas las 2.613 filas: **21 suben (8 a 3★, 13 a 4★), CERO bajan**, y 22 dejan de
#      emitir la frase falsa (la 22ª sigue en 2 estrellas por la OTRA rama, la de la espera).
#   3. EL RELOJ DE `info` Y DE `promo` ARRANCABA EN UN "HOLA". Las dos rubricas anclaban en el
#      PRIMER mensaje del cliente, sea lo que sea, y le cobraban al operador un tramo en el que
#      no habia nada que contestar. El criterio correcto ya estaba escrito en el docstring de
#      `info` ("hubo algo que responder") y `es_cortesia` ya se usa para esto en src/agilidad.py
#      (su confound 2); lo que faltaba era aplicarlo al ANCLA. Vive en
#      `signals.planteo_del_cliente`, compartido por las dos.
#          `info`   379 de 2.033 sesiones (18,6%) abren con cortesia -> **98 cambian: 60 suben, 38 bajan**
#          `promo`  658 de 10.163 (6,5%)                             -> **104 cambian: 57 suben, 47 bajan**
#      Casos: `58a51842`, donde la operadora mando el video tutorial UN minuto despues de la
#      consulta y la nota decia "Respondió recién 6,5 minutos después"; y `07b642b4`, con 8,6
#      HORAS en la nota y 6 SEGUNDOS de espera real.
#      QUE HAYA FILAS QUE BAJAN ES LA PRUEBA DE QUE ES UNA CORRECCION Y NO UNA INDULGENCIA: son
#      las de quien contesto rapido el saludo y tarde la consulta.
#      Y TIENE UN GUARD QUE COSTO CINCO FALSOS 1 ESTRELLA. Mover el ancla a un mensaje FINAL
#      sin respuesta manda la fila a "nadie le respondió", que es la nota mas cara del sistema,
#      y el vocabulario de cortesia es CERRADO -- todo lo que no esta en la lista parece un
#      planteo: `0ccb648c` cerraba con "super", `6d6f093b` con "Que dios los vendiga",
#      `0ebb1ecf` con "nada, tranqui". Si al planteo no le sigue ninguna respuesta, el ancla
#      vuelve donde estaba, asi que las 10 filas de 1 estrella de `promo` y las 9 de `info` no
#      se mueven ni para un lado ni para el otro.
#      QUEDA UN HUECO DECLARADO: 3 filas de `info` donde el cliente SI planteo al final
#      ("Necesito más información", "Tengo una consulta") y nadie contesto siguen con la nota
#      vieja, mas indulgente. Separarlas exige ampliar el vocabulario de cortesia, que es del
#      negocio. `deposito` y `retiro` son inmunes: anclan en un hecho del dominio (el
#      comprobante, el formulario), que ya es el planteo por construccion.
#
#   8. EL VOCABULARIO DE CORTESIA NO CONOCIA LOS TYPOS. `es_cortesia` decide si un bloque del
#      cliente PIDE algo, y de ahi cuelga el 1 estrella de agilidad ("un pedido quedo sin
#      respuesta"), que es el **89% de todos los 1 estrella del padron**. Clasificando los 442
#      pedidos abandonados de las 439 filas: **111 no tenian ningun pedido de verdad**. Once
#      formas de "gracias" mal escrito en 185 bloques (Graciad, Gracais, Graxias, Grqcias,
#      Gracis, Grx), mas el tratamiento con que el agente le habla al operador ("Gracias men",
#      "Listo amigo") y el acuse del que avisa que ya hizo su parte ("Ya le escribí").
#      LOS TYPOS SE RESUELVEN POR DISTANCIA DE EDICION, no agrandando la lista: un typo es una
#      tecla. El nucleo son TRES palabras (gracias/gracia/muchas) y NO todo el vocabulario,
#      porque se probo con todo y a una tecla de una palabra de cortesia hay palabras del
#      NEGOCIO: "llego" esta a una tecla de "luego", y "saldos" de "saludos".
#      Y LAS PALABRAS DE ACUSE VAN BAJO EL GUARD DE NEGACION que ya usa src/soporte.py: sin el,
#      `es_cortesia("no me llego")` daba True. "No me llegó" es el reclamo mas importante que
#      existe en este negocio y habria terminado en `sin_motivo`. Se sondeo ANTES de subirlo.
#      `que` y `paso` quedaron afuera de las dos listas: juntas hacen "que paso", que es una
#      pregunta y no tiene negacion que la delate.
#      OJO CON EL NUMERO: la clasificacion decia 111 y el arreglo devuelve **64**. La diferencia
#      es el precio de los dos guards de arriba, y se paga a proposito.
#   9. UN STICKER NO ES UN COMPROBANTE. `MEDIA_TYPES` lo incluia, y `is_real_media` es la fuente
#      unica de "esto es un adjunto de verdad" para CUATRO preguntas: si el agente pidio algo
#      (`es_pedido`), si el operador mando el comprobante (`operator_sent_media`), si hizo algo
#      por el caso (`_hubo_intento`) y si el cliente planteo algo (`client_sin_motivo`). Las
#      cuatro se contestaban mal. Un emoji en TEXTO ya lo trataba `es_cortesia`; esto alinea las
#      dos formas de mandar lo mismo. Efecto verificado uno por uno: mueve 1 fila de `deposito`
#      (`9e52f49d`, 4->2), y al mirarla el ancla estaba parada sobre un sticker de las 18:57 en
#      vez del comprobante real de las 18:54 -- o sea que ahora juzga el objeto correcto.
#
# TOTAL: **607 de 65.599 notas cambian (0,93%)** -- registro 203, agilidad 155, promo 110,
# info 107, soporte 21, deposito 11. NINGUNA baja a 1 estrella, que era el riesgo de sacar el
# sticker de `operator_sent_media` (esa señal PERDONA un pedido sin responder, asi que endurecerla
# podia fabricar 1 estrella nuevos: se midio y no paso).
#
# LA COBERTURA SI SE MUEVE, y se declara: **121 filas pasan de evaluada a `sin_motivo`** y 11 a
# `internal_notes_only`, porque el cliente no planteo nada -- que es exactamente para lo que
# existe ese skip. Sumadas a las 29 de agilidad que quedan sin nota (su unico pedido medible era
# una cortesia), son ~161 filas de 65.599 (0,25%) que dejan de tener nota. LAS 29 SON EL PROBLEMA:
# no van a `sin evaluar` sino al limbo de `eval_status='evaluated'` sin nota, que el tablero no
# muestra en ningun lado (ver el pendiente del `skip_reason` que falta).
# El arreglo del usted entra en `operator_resolved`, que decide el skip `customer_media_only`, asi
# que eso tambien habia que medirlo: recorridas las 5.387 salteadas, ni una pasa a evaluarse (el
# unico movimiento aparente son las 119 de `redireccion`, que el arnes no ve sin el mapa de lineas).
#
# EL PISO DE RUIDO DEL ARNES ES CERO, y por eso los numeros de arriba se leen directo:
# recalculadas las 10.163 filas de `promo` ANTES de tocarla, **cambian 0**. Para llegar a eso
# hubo que arreglar el arnes primero: `conversation_scores.resolved_at` NO es el `cierre_at` que
# recibio la rubrica -- worker.py lo reescribe despues de calificar con `tiempos_de(ventana)` --
# y con la columna equivocada aparecian 7 filas "cambiando" a 5 estrellas que no cambian. El
# valor correcto es `conversations.resolved_at` de la conversacion cuyo id es el session_id (ver
# PENDING_SESSIONS_SQL). El control post-arreglo sobre `deposito` da 7 de 9.536 (0,07%), y son
# sesiones que CRECIERON despues de scorearse: la copia se espejo con la corrida en curso.
# 2026.08-rubricas-v18 (2026-08-19). SALE DEL MANUAL DE OPERACIONES DE ATC, no de una
# auditoria de la data. Es el primer cambio de este repo que mueve una VARA en vez de corregir
# una medicion, y por eso el cuidado extra en documentarlo.
#
# EL HALLAZGO. El manual fija un solo numero para la primera respuesta y lo dice DOS veces
# (cap. 04 para jugadores, cap. 06 para agentes), con su razon tecnica: "cuando el mensaje
# ingresa a Whaticket, el sistema lo marca automaticamente como leido mediante el doble check
# azul. Por esta razon, la respuesta del operador debe ser inmediata y no superar un tiempo
# maximo de 1 MINUTO". Las seis rubricas deterministas usaban `AGIL = timedelta(minutes=2)`.
#
# DE DONDE SALIA EL 2, que es lo que vuelve indefendible mantenerlo: de la DISTRIBUCION
# OBSERVADA, y esta escrito en los propios docstrings -- info "mediana 1,5 min, 62,5% <=2 min",
# retiro "el 74,1% responde en <=2 min", deposito "el 78,0% acusa en <=2 min", promo "mediana
# 1,7 min, 56,8% <=2 min". Calibramos la vara contra lo que la gente ya hacia. Eso explica la
# concentracion en 4 y 5 estrellas (75,2% de las 65.599 notas) mejor que cualquier otra cosa:
# la escala se acomodo a la poblacion en vez de medirla contra la norma.
#
# IMPACTO MEDIDO recalculando las SEIS rubricas sobre la copia ENTERA -- son funciones puras
# sobre el transcript, asi que no hay muestreo: 52.002 sesiones comparables.
# **10.222 notas bajan (19,7%) y NINGUNA sube.**
#     agilidad        26.667 comparables   5.035 cambian (18,9%)   todas 5★ -> 4★
#     promo           10.053               1.980 (19,7%)           todas 4★ -> 3★
#     deposito         9.525               1.865 (19,6%)           1.840 de 4★, 25 de 5★
#     soporte_cuenta   2.592                 654 (25,2%)           624 de 4★, 30 de 5★
#     info             1.926                 447 (23,2%)           432 de 4★, 15 de 5★
#     retiro           1.239                 241 (19,5%)           239 de 4★, 2 de 5★
# Piso de ruido del arnes por debajo del 1% en cinco de las seis; `info` da 5,3% (107 filas
# que el recalculo no reproduce), asi que ahi la señal es 4x el ruido y no 20x como en el resto.
#
# LO QUE ESTE CAMBIO NO HACE. El manual trata el minuto como un MAXIMO -- pasarse es un
# incumplimiento -- y aca sigue siendo el borde de la banda ALTA. Un operador que contesta en
# 3 minutos sigue sacando 4 estrellas en agilidad y 3 en el resto. Convertir la escala en
# cumple/no-cumple es una decision del negocio, no un umbral, y NO se tomo.
#
# LO QUE NO SE TOCO, porque el manual no lo menciona: `retiro.ENTREGA_AGIL` (15 min para el
# comprobante), `retiro.ENTREGA_TOPE` (30), `registro.ENTREGA_AGIL` (5 min del traspaso de
# datos a las credenciales) y `agilidad.GAP_BLOQUE` (15 min, mecanica de armado de bloques y no
# una vara de calidad). Esos siguen calibrados con datos y siguen vigentes.
#
# LOS TEXTOS TAMBIEN CAMBIAN, y no es cosmetico: diez frases de `_COACHING` y de los rationale
# prometian "el objetivo son 2 minutos". Una fila que baja por pasarse del minuto con un consejo
# al lado que pide dos se desmiente sola -- la misma familia de bug que v16 pago caro. Las
# MEDICIONES historicas de los docstrings ("el 78,0% acusa en <=2 min") NO se tocaron: son
# evidencia de lo que se observo, no politica, y reescribirlas seria falsificar el registro.
# Contrato en tests/test_umbral_un_minuto.py (10 tests). 1.263 verdes desde 1.253.
# 2026.08-rubricas-v19 (2026-08-19). SEGUNDO cambio salido del manual de ATC, y el primero
# que corrige una FALSA ACUSACION nacida de una regla de negocio que el sistema no conocia.
#
# EL CASO. Manual cap. 06: "Si un jugador pertenece a un agente, el operador NO debe realizar
# recargas ni retiros". Lo correcto es derivarlo y DARLE EL TELEFONO del agente. La rubrica
# transaccional le exigia justo el paso prohibido -- confirmar la acreditacion -- porque
# clasifica por el TEMA de la charla (una recarga) y no sabe que existe la regla del agente.
# MEDIDO: 443 sesiones de `jugador` con derivacion; en `deposito` son 152 con media 3,08
# estrellas y 70 en 1-2 (46%). Leidos 3 de esos 70 en orden y sin elegir, los 3 son el
# procedimiento correcto castigado (`009312d9`, `03566bc9`, `09c1b759`).
#
# EL GATE EXIGE UN NUMERO AJENO, no la frase. La exencion no puede apoyarse en lo que el
# operador DICE: seria auto-otorgada y "derivalo al agente" pasaria a ser la forma de esquivar
# cualquier deposito. Se apoya en lo que HACE -- publicarle al cliente un numero que no es de
# ninguna de nuestras lineas --, que es un acto visible y auditable, y ademas es LO QUE EL
# MANUAL PIDE: derivar sin dar el telefono deja al cliente a la deriva y no cumple nada.
# Se descartaron MIDIENDO otras dos corroboraciones: la etiqueta del contacto
# (`AGENTE`/`JUGADOR AFILIADO`) cubre 5 de 74 casos (7%), y `users` no guarda telefono, asi
# que la BD NO PUEDE decir si un operador es ademas agente.
#
# EL RELOJ DE LA RAMA SON 5 MINUTOS Y NO EL MINUTO DE v18, y la razon es del manual: antes de
# derivar el operador TIENE que pedir el usuario y verificar la agencia (cap. 05, pasos 1 y 2).
# Eso es una consulta, no un reflejo, y cobrarle el minuto seria cobrarle la verificacion que
# el manual le exige. Mismo tope que la rama del alta imposible de src/registro.py. Sobre los
# 18 casos que la señal encuentra: p50 4,3 min, 56% dentro de 5 y solo 11% dentro de 1.
#
# IMPACTO sobre las 10.777 sesiones deterministas de deposito+retiro: **13 filas SUBEN
# (9 de 2★ a 4★, 4 de 2★ a 3★) y NINGUNA baja.** Es chico a proposito: el gate es conservador
# y cubre 18 de las 74 castigadas (24%). Las otras 56 no dieron numero (43% de la poblacion,
# o sea que tampoco cumplieron el procedimiento completo) o apuntaron a una linea NUESTRA
# (26%, que es `redireccion` y tiene su propia regla).
# OJO CON EL CONTROL: el arnes reporta 19,66% de filas que no reproducen la nota guardada.
# NO es ruido, es el delta de v18 -- las notas guardadas se calcularon con AGIL=2 min. Coincide
# con el 19,6% que v18 midio para `deposito`. La comparacion antes/despues de ESTA rama corre
# con AGIL=1 en las dos puntas, asi que las 13 estan bien aisladas.
#
# TECHO EN 4 en las dos rubricas, igual que la rama del rechazo: el 5 es "el mejor escenario
# del motivo" y una transaccion que ATC no podia hacer no lo es.
# Contrato en tests/test_derivacion_al_agente.py (17 tests). 1.280 verdes desde 1.263.
# 2026.08-rubricas-v20 (2026-08-19). TERCER cambio del manual de ATC: la politica del ultimo
# mensaje. Cap. 04 y cap. 06, TEXTUAL en los dos: "Es politica obligatoria del departamento
# que el ultimo mensaje de la conversacion sea enviado por el operador". Y sobre el caso
# chico: si el cliente responde con un "gracias", emoji o sticker despues de la despedida,
# "el operador debera responder para mantener el estandar de cierre".
#
# NO BAJA NOTAS: BLOQUEA LA QUINTA ESTRELLA. El 5 de las cuatro rubricas que lo dan por el
# cierre dice, literal, "antes de cerrar se aseguró de que no le faltara nada", y eso no se
# puede afirmar de una sesion donde el cliente contesto esa pregunta y nadie le respondio. La
# fila se desmentiria sola. Es un TECHO en 4, no un castigo: el trabajo se hizo.
#
# EL GATE DEL CIERRE ES LO QUE LA VUELVE JUSTA. Solo cuenta si el cliente quedo colgado con el
# ticket TODAVIA ABIERTO. MEDIDO: de las 659 sesiones de 5 estrellas que terminan con el
# cliente, **548 (83%) escribieron DESPUES de `resolved_at`** -- ahi el operador ya mando
# /FIN, espero sus 5 minutos y cerro, que es el procedimiento del manual. Quedan 111.
#
# LA PROPORCION: solo el 4,1% de las 65.588 sesiones evaluadas termina con el cliente, asi que
# el cumplimiento de esta politica YA ES ALTO. Y lo que el cliente manda es casi siempre una
# cortesia: "gracias" 620, "muchas gracias" 202, "ok" 157, sticker 72, "listo" 67 -- justo el
# caso que el manual obliga a contestar.
#
# IMPACTO: **9 filas bajan de 5 a 4** (deposito 3, info 4, soporte_cuenta 2) y ninguna sube.
# POR QUE TAN POCAS, que es lo interesante: de las 111 candidatas, 67 estan en las rubricas
# deterministas por motivo... pero eran 5 estrellas CALCULADAS CON `AGIL=2`. v18 ya las bajo
# sola a 3 o 4, asi que no queda quinto que bloquear. Los tres cambios del manual INTERACTUAN
# y hay que medirlos en orden, no por separado. Las otras 44: 37 en `agilidad` y 7 en el
# camino LLM.
#
# QUEDAN AFUERA A PROPOSITO:
#   - `agilidad` (37 filas). La politica del manual tambien cubre a los agentes, pero esa
#     rubrica mide UNA cosa por diseño -- cuanto tardo el operador -- y meterle un eje de
#     cortesia la convierte en otra cosa. Entrarla es decision del negocio.
#   - `promo`. Su quinta estrella no se gana con el cierre sino con el MATERIAL, asi que
#     agregarle esta compuerta seria una regla nueva y no la correccion de una que miente.
#   - el camino LLM (7 filas). Ahi la etiqueta la deriva `label_from_facts` y esto seria un
#     hecho mas; entra cuando se toque esa capa.
# Contrato en tests/test_ultima_palabra.py (11 tests). 1.291 verdes desde 1.280.
# 2026.08-rubricas-v21 (2026-08-19). EL TABLERO PASA A HABLAR EL IDIOMA DE ATC. No cambia
# ninguna nota: cambia COMO se nombran los errores y con que vocabulario se aconseja. Pedido
# del negocio, y con razon -- el tablero lo leen ellos.
#
# EL PROBLEMA, MEDIDO. `errores[]` lo llenaba el LLM en TEXTO LIBRE: **7.019 errores emitidos
# en 3.680 TEXTOS DISTINTOS (52% unicos)**. La misma falta escrita de cinco formas:
#     "No se pidieron los datos necesarios para crear la cuenta."           464
#     "No se solicito al cliente los datos necesarios para crear la cuenta." 115
#     "No se pidio al cliente los datos necesarios para crear la cuenta."    115
#     "No se le pidio al cliente los datos necesarios para crear la cuenta."  49
# Son la misma falta contada cuatro veces. Un supervisor no puede decir "esta semana tuvimos
# 40 de este error", ni comparar dos operadores, ni ver si algo mejora. El campo existia y no
# servia para nada.
#
# LA SOLUCION YA ESTABA ESCRITA, Y NO POR NOSOTROS. El manual de ATC publica dos listas
# CERRADAS Y NUMERADAS -- doce errores criticos y doce buenas practicas -- y aclara que
# cualquiera de los errores "puede derivar en medidas correctivas". Esa es la rubrica del
# negocio con las palabras del negocio. Ahora:
#   - `src/catalogo_atc.py` la guarda VERBATIM (regla del modulo: el campo `texto` no se
#     edita), con la numeracion de ellos, porque el supervisor los conoce por numero.
#   - el pase con LLM emite CODIGOS (E01-E12) en vez de prosa: el prompt le da la lista con
#     numero Y frase, y el schema los declara como ENUM CERRADO, asi que el grammar del
#     nivel 2 lo vuelve imposible de violar.
#   - `/api/catalogo` se lo sirve al tablero UNA vez. No se duplica en el JS: tener el
#     vocabulario dos veces garantiza que un dia digan cosas distintas.
#   - el modal muestra "Errores criticos · manual ATC" con el numero adelante y la frase del
#     manual, y el "por que" del manual como tooltip. "Lo que hizo bien" pasa a ser "Buenas
#     practicas cumplidas", que es como el manual las llama.
#
# LAS 65.599 FILAS YA SCOREADAS NO SE PIERDEN: traen prosa libre y el tablero cae al texto
# crudo cuando el codigo no esta en el catalogo. Un rescore no puede ser requisito para
# cambiar una pantalla.
#
# EL COACHING TAMBIEN CAMBIA DE VOCABULARIO. El manual tiene una respuesta rapida con NOMBRE
# para casi cada consejo que damos, y decir "/R2verificaciondeboleta apenas entra el
# comprobante" no deja nada que interpretar -- el operador la tiene en Whaticket. Reescritos
# los consejos de mayor volumen de `agilidad` y `deposito` con /Bienvenida, /R2verificaciondeboleta,
# /R3Recarga y /FIN. Y hay un test nuevo que prohibe nombrar una plantilla que NO este en el
# catalogo: mandar al operador a buscar algo que no existe seria, ademas, el error critico
# E10 del propio manual ("Alterar respuestas rapidas... o informacion oficial").
#
# LO QUE FALTA PARA CERRAR EL CIRCULO, y es un pedido concreto al negocio: el manual nombra
# las respuestas rapidas pero NO incluye su TEXTO CANONICO. Con esos textos se puede auditar
# E10 de verdad (¿la plantilla salio sin modificar?) y ademas se vuelve medible el protocolo
# de seguimiento que quedo afuera en v20.
# Contrato en tests/test_catalogo_atc.py (10) y tests/test_coaching.py. 1.302 verdes desde 1.291.
SCORING_VERSION = "2026.08-rubricas-v21"

# =============================================================================
# Forma CANÓNICA de conversation_scores (grano SESIÓN, todas las columnas
# actuales). store.py es la FUENTE de esta forma; db/scores_schema.sql debe
# mantenerse en sync con estas sentencias.
#
# Sin BEGIN/COMMIT ni ALTER de retrocompat: es la tabla FRESCA que crea la
# migración "desde cero con backup". No lleva `%` para no colisionar con el
# paramstyle de psycopg. Idempotente por CREATE ... IF NOT EXISTS.
# =============================================================================
_CREATE_SCORES_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_scores (
    conversation_id         uuid PRIMARY KEY,
    account                 text NOT NULL,
    ticket_id               uuid,
    segment                 text,
    queue_name              text,
    channel                 text,
    user_id                 uuid,
    user_name               text,
    conversation_created_at timestamptz,
    resolved_at             timestamptz,

    rubric                  text NOT NULL,
    eval_status             text NOT NULL,
    skip_reason             text,

    first_response_seconds  numeric,
    resolution_seconds      numeric,
    message_count           integer,
    agent_message_count     integer,
    bot_message_count       integer,
    contact_message_count   integer,
    was_unassigned          boolean,

    dimensions              jsonb,
    llm_model               text,

    rating_label            text,
    rating_rationale        text,

    resultado               text,
    deposit_count           integer,

    stars                   numeric,
    stars_breakdown         jsonb,

    is_estimate             boolean NOT NULL DEFAULT true,
    scoring_version         text,
    scored_at               timestamptz NOT NULL DEFAULT now(),

    atencion                text,
    deposit_observed        boolean,
    deposit_mismatch        boolean,
    session_id              uuid,
    -- Pase v2: motivo de la interaccion clasificado por el LLM (deposito, retiro,
    -- soporte_cuenta, info, promo, registro, problema). NULL en filas skipped o del
    -- pase viejo. Sin CHECK: la validez la garantiza el enum del schema del scorer.
    motivo                  text,

    -- rating_applicable: LEGACY de la Opción B (adquisición sin rating). v2 la retiró
    -- (promo/registro se califican por su motivo). Queda como true en toda fila
    -- scoreada; se conserva por compatibilidad con queries/dashboard.
    rating_applicable       boolean NOT NULL DEFAULT true,

    CONSTRAINT chk_rubric      CHECK (rubric IN ('human', 'bot')),
    CONSTRAINT chk_eval_status CHECK (eval_status IN ('evaluated', 'skipped')),
    CONSTRAINT chk_eval_coherence CHECK (
        (eval_status = 'skipped'   AND stars IS NULL     AND skip_reason IS NOT NULL) OR
        (eval_status = 'evaluated' AND skip_reason IS NULL)
    ),
    CONSTRAINT chk_stars_range CHECK (stars IS NULL OR (stars >= 1 AND stars <= 5))
)"""

# Índices de db/scores_schema.sql + idx por session_id (grano sesión).
_SCORES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_scores_account_segment ON conversation_scores (account, segment)",
    "CREATE INDEX IF NOT EXISTS idx_scores_user            ON conversation_scores (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_scores_created         ON conversation_scores (conversation_created_at)",
    "CREATE INDEX IF NOT EXISTS idx_scores_rubric_status   ON conversation_scores (rubric, eval_status)",
    "CREATE INDEX IF NOT EXISTS idx_scores_session         ON conversation_scores (session_id)",
)

# Nombre del backup de la tabla previa (grano conversación) que deja la migración.
_SCORES_BACKUP_TABLE = "conversation_scores_pre_session"


def _create_fresh_scores(cur) -> None:
    """Crea la tabla fresca conversation_scores + índices (idempotente)."""
    cur.execute(_CREATE_SCORES_TABLE)
    for stmt in _SCORES_INDEXES:
        cur.execute(stmt)


def ensure_session_scoring_migration(cur) -> dict:
    """Migración AUTOMÁTICA e IDEMPOTENTE "desde cero con backup".

    Al arrancar el servicio: renombra la tabla vieja conversation_scores a un
    backup (`conversation_scores_pre_session`) y crea una tabla FRESCA de grano
    sesión, para empezar el scoring de cero SIN perder lo anterior.

    Idempotente: el gate es la EXISTENCIA del backup.
      - Sin backup + tabla vieja presente -> RENAME + crea fresca. migrated=True.
      - Sin backup + sin tabla vieja (install nueva) -> solo crea fresca. migrated=False
        (no había nada que respaldar, no fue una migración real).
      - Con backup (ya migrado) -> NO re-renombra (no destruye); solo asegura la
        fresca (CREATE IF NOT EXISTS). migrated=False.

    Devuelve {"migrated": bool}; True SOLO cuando efectivamente renombró.
    """
    # Lock de transacción: dos workers arrancando a la vez (rolling deploy) podrían
    # competir en el RENAME. El advisory lock serializa la migración; se libera solo
    # al commit de la transacción del caller. El 2do worker espera y ve el backup ya
    # creado -> no re-renombra.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('conversation_scores_migration'))")
    cur.execute(f"SELECT to_regclass('{_SCORES_BACKUP_TABLE}')")
    backup = cur.fetchone()[0]
    if backup is None:
        cur.execute("SELECT to_regclass('conversation_scores')")
        old = cur.fetchone()[0]
        migrated = old is not None
        if migrated:
            cur.execute(
                f"ALTER TABLE conversation_scores RENAME TO {_SCORES_BACKUP_TABLE}"
            )
            # RENAME TABLE NO renombra los indices: quedan con sus nombres canonicos
            # pegados al backup, y el CREATE INDEX IF NOT EXISTS de la fresca los
            # saltearia (colision de nombre) dejandola SIN indices -> dashboard lento.
            # Liberamos los nombres canonicos renombrando los indices del backup.
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s",
                (_SCORES_BACKUP_TABLE,),
            )
            for (idxname,) in cur.fetchall():
                if not idxname.endswith("_presess"):
                    cur.execute(f'ALTER INDEX "{idxname}" RENAME TO "{idxname}_presess"')
        _create_fresh_scores(cur)
        return {"migrated": migrated}
    # Ya migrado: no tocar el backup ni la fresca existente, solo asegurar forma.
    _create_fresh_scores(cur)
    return {"migrated": False}

_COLUMNS = (
    "conversation_id", "account", "ticket_id", "segment", "queue_name", "channel",
    "user_id", "user_name", "conversation_created_at", "resolved_at",
    "rubric", "eval_status", "skip_reason",
    "first_response_seconds", "resolution_seconds",
    "message_count", "agent_message_count", "bot_message_count",
    "contact_message_count", "was_unassigned",
    "dimensions", "llm_model", "rating_label", "rating_rationale",
    "stars", "stars_breakdown", "deposit_count", "is_estimate", "scoring_version",
    "atencion", "deposit_observed", "deposit_mismatch", "session_id",
    "rating_applicable", "motivo",
)

# Columnas nuevas del pase LLM unificado. ensure_scores_columns() las agrega a una
# tabla de prod ya creada (el CREATE ... IF NOT EXISTS no agrega columnas). Mismo
# patron self-healing que conversions.ensure_table.
_SCORES_COLUMN_TYPES = (
    ("atencion", "text"),
    ("deposit_observed", "boolean"),
    ("deposit_mismatch", "boolean"),
    ("session_id", "uuid"),
    ("rating_applicable", "boolean NOT NULL DEFAULT true"),
    ("motivo", "text"),
)


def ensure_scores_columns(cur) -> None:
    """Agrega las columnas del pase LLM unificado si faltan (idempotente)."""
    for col, coltype in _SCORES_COLUMN_TYPES:
        cur.execute(
            f"ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS {col} {coltype}"
        )


def build_score_record(
    *,
    conversation: dict,
    stats: MessageStats,
    rubric: str,
    eval_status: str,
    skip_reason: str | None,
    score: ScoreResult | None,
    operator_id=None,
    operator_name: str | None = None,
    deposit_count: int = 0,
    deposit_gate: bool | None = None,
    session_id=None,
    scoring_version: str = SCORING_VERSION,
) -> dict[str, Any]:
    """Arma el dict de columnas para conversation_scores.

    `operator_id`/`operator_name` = operador reconstruido desde los mensajes (el
    conversations.user_id suele venir NULL). was_unassigned refleja el flag de
    asignacion de whaticket (conversations.user_id).
    """
    c = conversation
    segment = segment_for_queue(c.get("queue_name"))
    record: dict[str, Any] = {
        "conversation_id": c["id"],
        "account": c.get("account"),
        "ticket_id": c.get("ticket_id"),
        "segment": segment,
        "queue_name": c.get("queue_name"),
        "channel": c.get("channel"),
        "user_id": operator_id,
        "user_name": operator_name,
        "conversation_created_at": c.get("created_at"),
        "resolved_at": c.get("resolved_at"),
        "rubric": rubric,
        "eval_status": eval_status,
        "skip_reason": skip_reason,
        "first_response_seconds": first_response_seconds(
            c["created_at"], c.get("first_sent_message_at")
        ),
        "resolution_seconds": resolution_seconds(c["created_at"], c.get("resolved_at")),
        "message_count": stats.message_count,
        # La COLUMNA conserva el nombre legacy `agent_message_count`; el atributo de
        # MessageStats ya es `operator_message_count` (ver src/metrics.py). Renombrar la
        # columna exigiria migrar conversation_scores sin ganancia visible: nadie la ve.
        "agent_message_count": stats.operator_message_count,
        "bot_message_count": stats.bot_message_count,
        "contact_message_count": stats.contact_message_count,
        "was_unassigned": was_unassigned(c.get("user_id")),
        "dimensions": None,
        "llm_model": None,
        "rating_label": None,
        "rating_rationale": None,
        "stars": None,
        "stars_breakdown": None,
        "deposit_count": deposit_count,
        "is_estimate": True,
        "scoring_version": scoring_version,
        # Pase LLM unificado. En el path por-conversacion session_id llega None (lo
        # llena el paso 2). atencion/deposit_observed solo si hubo score.
        "atencion": None,
        "deposit_observed": None,
        "deposit_mismatch": _deposit_mismatch(deposit_count, score, deposit_gate),
        "session_id": session_id,
        # Motivo v2: lo llena el score (score_by_motivo). None en skipped / pase viejo.
        "motivo": None,
        # v2: el rating (por MOTIVO) aplica SIEMPRE que haya evaluación. Se retiró la
        # supresión Opción B en adquisición: promo/registro tienen su propia rúbrica y
        # SÍ se califican. Columna conservada (siempre true en filas scoreadas) por
        # compatibilidad con queries/dashboard.
        "rating_applicable": True,
    }
    if score is not None:
        record.update(
            llm_model=score.llm_model,
            atencion=score.atencion,
            deposit_observed=score.deposit_observed,
            motivo=score.motivo,
            dimensions={**score.dimensions,
                        "recomendacion": score.recomendacion,
                        # Los CODIGOS al lado del texto: el texto es para leer, el codigo es
                        # para CONTAR. Ver src/catalogo_coaching.py. Lista vacia en las
                        # rubricas que todavia no se migraron.
                        "recomendacion_codigos": score.recomendacion_codigos,
                        # A que buena practica del manual apunta el consejo. Es lo que hace
                        # sumable el coaching por practica en los DOS caminos.
                        "recomendacion_practica": score.recomendacion_practica},
            rating_label=score.rating_label,
            rating_rationale=score.rating_rationale,
            stars=score.stars,
            stars_breakdown={
                "rubric": score.rubric,
                "label": score.rating_label,
                "stars": score.stars,
                "scoring_version": scoring_version,
                "floored": score.floor_applied,
            },
        )
    return record


def _deposit_mismatch(deposit_count: int, score: ScoreResult | None,
                      deposit_gate: bool | None = None) -> bool | None:
    """Reconciliacion determinista vs observacion del deposito (senal de calidad de dato).

    None si no se puede reconciliar (sin score o sin observacion del deposito).
    Si no: True cuando el gate determinista y la observacion discrepan. El determinista
    manda; el flag solo marca la discrepancia.

    `deposit_gate` = la respuesta de LAS DOS PUERTAS de `deposito.es_transaccion`. HAY QUE
    COMPARAR LA MISMA PUERTA: `deposit_count` sale de `deposit_candidate_count`, que exige
    que el CLIENTE escriba una palabra de recarga -- justo lo que la puerta 2 (el operador
    acusa el comprobante) existe para NO exigir. Sin esto, todo deposito que entra por la
    puerta 2 tiene `deposit_count=0` por construccion y el flag disparaba SIEMPRE.
    MEDIDO el 2026-08-12: 889 de 2.200 filas de `deposito` (40,4%), el 100% con
    `deposit_count=0`. La nota estaba bien; el indicador del dashboard mentia.
    None -> se degrada al criterio viejo, para no cambiar el path por conversacion.
    """
    if score is None or score.deposit_observed is None:
        return None
    determinista = (deposit_count > 0) if deposit_gate is None else deposit_gate
    return determinista != score.deposit_observed


# Columnas JSONB que hay que envolver para psycopg.
_JSONB_COLS = {"dimensions", "stars_breakdown"}


def upsert_score(cur, record: dict) -> None:
    """Inserta o actualiza la fila por conversation_id (idempotente)."""
    cols = list(_COLUMNS)
    placeholders = ", ".join(f"%({col})s" for col in cols)
    updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in cols if col != "conversation_id")
    sql = (
        f"INSERT INTO conversation_scores ({', '.join(cols)}, scored_at) "
        f"VALUES ({placeholders}, now()) "
        f"ON CONFLICT (conversation_id) DO UPDATE SET {updates}, scored_at = now()"
    )
    params = {
        col: (Jsonb(record[col]) if col in _JSONB_COLS and record[col] is not None else record[col])
        for col in cols
    }
    cur.execute(sql, params)
