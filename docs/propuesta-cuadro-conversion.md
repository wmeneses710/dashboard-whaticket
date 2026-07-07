# Propuesta — 4.º cuadro: Evolución por operador (conversión vs. atención pasiva)

**Estado:** pendiente de decisión de negocio (no es un bloqueo técnico).
**Fecha del análisis:** 2026-07-07.

## Resumen

El dashboard tiene hoy **3 cuadros canónicos** alineados con el análisis (`ANALISIS_REDES_documentacion.md`, gráficos de la línea 205):

1. Nuevos jugadores vs. % depósito por mes (`nuevos_jugadores.png`)
2. % depósito en WhatsApp por operador (`evolucion_deposito_wa.png`)
3. Carga mensual por operador (`carga_operador_mes.png`)

Falta el 4.º gráfico canónico: **`evolucion_operadores.png`** — evolución mensual por
operador con **% conversión** (verde) y **% atención pasiva** (rojo), en formato de
recuadros pequeños (un panel por operador).

Este documento explica por qué no se puede construir con el pipeline actual y cuál es
el costo real de agregarlo.

## Qué mide el cuadro del análisis

Los dos porcentajes provienen de una clasificación LLM dedicada (`analizar_grande.py`),
que le hacía al modelo una pregunta **de conversión** por cada conversación y devolvía:

| Campo | Valores |
|---|---|
| `resultado` | cuenta_creada · recarga_confirmada · interesado_sin_cerrar · se_enfrio · no_interesado · otro_tema |
| `enganche` | alto · medio · bajo |
| `calidad_atencion` | empujo_al_registro · **paso_pasivo** · no_respondio_bien |

De ahí:
- **% conversión** = conversaciones con `resultado` ∈ {`cuenta_creada`, `recarga_confirmada`}.
- **% atención pasiva** = conversaciones con `calidad_atencion` == `paso_pasivo`.

## Por qué hoy no se puede reproducir

Son dos motivos independientes; ambos hay que resolverlos.

### 1. El scorer mide otro eje

El scorer del dashboard evalúa **calidad de atención** (empatía, claridad, resolución,
tono → estrella 1-5). Nunca pregunta si el jugador **convirtió** ni si el operador
**empujó o fue pasivo**. Son ejes perpendiculares: un operador puede tener 5 estrellas de
calidad y ser pasivo en conversión. La columna `resultado` de `conversation_scores` existe
pero está vacía; no hay ninguna señal de `calidad_atencion`.

### 2. Cobertura: el LLM cubre una fracción mínima

Los otros 3 cuadros son *full-scale* (SQL sobre todas las conversaciones). El 4.º
dependería del LLM, que solo corre sobre lo scoreado, y hoy la cobertura es ínfima:

| Cuenta | Jugador total | Scoreadas | Cobertura |
|---|---|---|---|
| sistemas | 36.287 | 169 | 0,5 % |
| datos | 3.494 | 319 | 9 % |

El cuadro del análisis tenía **n ≈ 800+ por operador**. Con lo scoreado hoy habría ~2-3
conversaciones por operador-mes → estadísticamente es ruido, no señal. El worker de
scoring corre incremental (se pone al día de a poco); `analizar_grande.py`, en cambio, fue
un batch dedicado que clasificó las 25k de una sola vez.

## Costo real

El gate **no** es cambiar el schema (la tabla no está en producción; agregar columnas es
trivial). El costo real es **clasificar con el LLM casi todas las ~40k conversaciones
jugador** (no las ~500 actuales). A ~3s por conversación con `qwen3.5:4b`, es del orden de
**30+ horas de LLM en batch**.

## Opciones de implementación

### (a) Extender el scorer de calidad + subir cobertura al 100 %
Agregar `resultado` + `calidad_atencion` al prompt del scorer, truncar
`conversation_scores` y re-scorear todo el segmento jugador.
- Contra: el scorer es pesado (contexto del hilo + 4 dimensiones + rationale); mezclar dos
  tareas en un prompt encarece cada llamada y puede degradar ambas clasificaciones.

### (b) Pase de conversión dedicado y full-scale — **recomendada**
Subsistema aparte (clon de `analizar_grande.py`): prompt de conversión simple, sin contexto
del hilo, `format:json`, en su propia tabla, batch sobre las ~40k jugador.
- A favor: fiel al análisis, más barato por conversación, desacoplado del scorer de calidad;
  no requiere tocar `conversation_scores`.

## Recomendación

El 4.º cuadro es un **subsistema nuevo** (clasificación de conversión full-scale), no un
ajuste de gráfico. Es viable y la opción (b) es la correcta. La decisión de avanzar es de
negocio: ¿vale correr el batch de ~30 h para tener el gráfico de conversión/pasividad por
operador? Mientras tanto, los 3 cuadros actuales son honestos y suficientes.
