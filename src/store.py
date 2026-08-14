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
SCORING_VERSION = "2026.08-rubricas-v16"

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
            dimensions={**score.dimensions, "recomendacion": score.recomendacion},
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
