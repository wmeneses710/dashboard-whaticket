# Diseño — Modelo de evaluación v2 (scoring por motivo)

Estado: PROPUESTA (validada sobre datos reales de la copia). No implementar hasta
aprobación. Prerrequisito ya resuelto: fix de freshness de `end_at` (commit 795276b).

## 1. Problema

El scoring actual elige la rúbrica por QUIÉN atendió (`decide_rubric`: humano vs
bot) y mide siempre calidad de SOPORTE ("¿resolviste el problema?"). Consecuencias
verificadas:

- No distingue el MOTIVO del cliente. Una recarga, un retiro y una consulta se
  miden con la misma vara de soporte.
- `atencion` (empujó/pasivo/no_respondió) se emite como label aparte y NO influye
  en la nota (no está en `required` del schema).
- La Opción B (adquisición) suprime el rating entero en vez de medir con otra vara
  → regresión: antes al menos intentaba evaluar.
- La eficiencia transaccional ("listo"/"ing") se lee como desatención y se castiga,
  cuando es el mínimo correcto.

## 2. Objetivo

La vara depende del MOTIVO de la interacción, sobre la SESIÓN completa (no el chat
suelto). La atención pasa a ser un INSUMO de la nota, ponderado por motivo. Casos
sin contexto real (troll, sin texto legible ni comprobante) no se evalúan.

## 3. Taxonomía de motivos (derivada de datos)

Distribución a grano sesión (copia): sistemas = operación; datos = adquisición.

| Motivo | sistemas | datos | Qué es |
|---|---|---|---|
| depósito/recarga | 45,1% | 6,9% | ingreso de plata (comprobante + acreditar) |
| retiro | 6,1% | 0,6% | solicitud de salida de plata + comprobante |
| soporte de cuenta | (dentro de otro/info) | — | contraseña, cambio de cuenta/nombre, KYC |
| info/consulta | 10,3% | 10,3% | saldo, cómo/cuándo/cuánto, dudas |
| promo/bono | 5,3% | 38,6% | respuesta a campaña / interés comercial |
| registro/activación | 2,5% | 13,0% | alta de cuenta |
| problema/reclamo | 1,5% | 0,7% | no se acreditó, error, "no pagan" |
| troll / sin contexto | ~0,08% | — | ofensivo o sin intención evaluable → SKIP |

## 4. Detección del motivo

Sobre la SESIÓN mergeada (no la conversación suelta), porque los chats sueltos son
fragmentos (saludos, "gracias", templates web del botón del sitio).

Entradas:
- Primer mensaje SUSTANTIVO del cliente (se saltean saludos, templates de entrada
  web tipo "hola, te escribo desde sorti.ec", y fragmentos de una palabra).
- Señal determinista de depósito (`deposit_count` / comprobante como media). El
  motivo depósito NO depende solo del texto: el 53% de operación es media.

El motivo lo clasifica el LLM en el mismo pase unificado (un campo nuevo `motivo`),
apoyado por las señales deterministas cuando existen.

## 5. Modelo de scoring: DOS CAPAS

### Capa 1 — PISO (por motivo): ¿resolvió el motivo?
Mínimo eficiente y correcto = 3 (aceptable). Debajo (error, ignoró, info mal,
maltrato, cierre rápido sin resolver) = 1-2.

### Capa 2 — UPLIFT (universal): ¿hizo algo MÁS?
Acción extra de negocio + buena atención (saludo, elección de palabras,
cordialidad) sube a 4-5.

| Motivo | Piso (3) | Uplift (4-5) |
|---|---|---|
| depósito/recarga | acreditar correcto + confirmar ("ing"/"listo"), templateado ok | personalizar, mencionar bonos, invite al canal, velocidad |
| retiro | procesar + comprobante (ver §7) | invitar a volver a depositar (retención), personalizar |
| soporte de cuenta | resolver o guiar el trámite de cuenta | acompañar, confirmar la solución |
| info/consulta | responder correcto y completo | convencer y llevar a depósito |
| promo/bono | informar la promo | empujar el registro/depósito concreto |
| registro/activación | guiar el alta | cerrar el alta + primer depósito |
| problema/reclamo | resolver o escalar bien | seguimiento, disculpa proactiva |

### Atención como eje de uplift
`empujó` = hizo la acción extra → sube (4-5). `pasivo` = solo el piso → queda en 3.
`no_respondió` = debajo del piso → 1-2. La atención se PONDERA por motivo: fuerte en
conversión (promo/registro/info), presente como retención en transaccional.

## 6. Corte de sesión: por AGENTE

Decisión confirmada: el handoff entre agentes corta la sesión. Cada agente se
evalúa en su propia sesión (atribución limpia). No cambia respecto de hoy
(`assign_sessions` ya corta por cambio de agente); se documenta como intencional.

## 7. Señales deterministas

- **Duración / cierre rápido**: `end_at - start_at` (confiable gracias al fix de
  freshness). Una sesión que cierra muy rápido (< ~10 min) sin resolución es un
  FLAG diagnóstico. Matiz: el auto-close agresivo es deficiencia de configuración
  del sistema, no necesariamente culpa del agente → flag, no penalización directa.
- **Depósito**: gate determinista (`deposit_count`) manda sobre `deposit_observed`
  del LLM. El motivo depósito y su piso se apoyan en esta señal; el LLM no debe
  fallar por "media ilegible" si el gate registró el comprobante.
- **Retiro sin comprobante visible**: el comprobante se manda "en breve" (media
  posterior) y puede caer fuera de la ventana. NO penalizar por "no vi comprobante"
  si el retiro fue procesado y la señal lo respalda.

## 8. Reglas de skip (no evaluar)

- Troll / ofensivo sin intención evaluable.
- Cliente solo mandó media sin comprobante reconocible ni texto legible.
- Solo notas internas.
- Sin respuesta del negocio (se mantiene, pero con el fix de freshness deja de
  disparar en falso por scores viejos).

## 9. Impacto en el código

- `router.py`: `decide_rubric` deja de decidir por handler; la rúbrica se elige por
  MOTIVO. `decide_eligibility` suma skip de troll/sin-contexto.
- `rubrics.py`: una `RubricSpec` por motivo (piso + criterios de uplift).
- `prompts.py`: el pase unificado clasifica `motivo` y aplica la rúbrica del motivo;
  la atención entra en la nota (deja de ser best-effort suelto).
- `store.py`: columna `motivo`; `atencion` pasa a insumo del rating.
- Schema de salida: `motivo` y `atencion` en `required`.
- Dashboard: mostrar el motivo; el flag de cierre rápido.

## 10. Reproceso

Bump `SCORING_VERSION`. Backfill newest-first (el worker ya re-abre por `end_at`).
Un solo reproceso: montar junto con el fix de freshness ya commiteado.

## 11. Abiertos

- Ubicación exacta de "soporte de cuenta" en la detección (motivo propio, ya
  decidido; falta el lexicón/criterio del LLM).
- Umbral del flag de cierre rápido (¿10 min? ¿por motivo?).
- Ponderación fina piso/uplift por motivo (calibrar contra el 14b con los ejemplos
  etiquetados, no el 4b).
