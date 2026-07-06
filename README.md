# dashboard-whaticket

Evaluacion de calidad de atencion y dashboard sobre las conversaciones que
captura el ETL de Whaticket (proyecto hermano `ETLWhaticket`).

No re-captura datos: **lee la misma BD Postgres** que llena el ETL, puntua cada
conversacion con un LLM local (Ollama) y sirve un dashboard HTML interactivo.

## Que hace

1. **Metricas objetivas** (SQL, sin LLM): tiempo a primera respuesta, tiempo de
   resolucion, nº de mensajes, reaperturas, conversaciones sin asignar.
2. **Scoring semantico** (LLM local `qwen3.5:4b`): rubrica segun QUIEN respondio
   de verdad, detectado por `messages.sent_from` (`CHATBOT` = bot, resto =
   operador humano) — NO por `conversations.user_id` (que suele venir NULL aunque
   haya atendido una persona). En la practica casi todo es `human`: solo el
   ~0.04% son conversaciones de bot puro. El operador se reconstruye desde
   `messages.user_id`. El LLM emite una **calificacion cualitativa** (una
   etiqueta + su porque contextual), no un numero. Ver `src/rubrics.py`,
   `src/router.py` y `src/prompts.py`.
3. **Calificacion por estrellas (1-5)**: traduccion **determinista** de la
   etiqueta cualitativa (tabla que controlamos en `src/rubrics.py`), NO un
   promedio ni una salida del LLM (los modelos clasifican bien pero calibran
   mal los numeros). Es una **ESTIMACION**, no un dato de la plataforma
   (Whaticket trae `csat` vacio). El dashboard lo marca como tal.
4. **Dashboard unico**: una sola vista que filtra por cuenta / cola / agente /
   canal (no un dashboard por cuenta).

## Principio de diseño (no negociable)

> La estrella mide lo que el agente **controla** (tiempo, forma, contexto,
> resolucion). El **resultado de negocio** (deposito/conversion) va aparte,
> NUNCA dentro de la estrella.

Motivo: el deposito depende del canal/trafico, no del agente. En Facebook los
operadores empujan mas y el jugador igual no deposita. Calificar por deposito
castiga injustamente a quien atiende el canal malo. (Leccion del analisis previo,
ver `../ANALISIS_REDES_documentacion.md`.)

## Segmentacion

Por **cola** (`queueName`), no por cuenta. Cada segmento usa una rubrica distinta:

| Segmento  | Colas | Se mide |
|-----------|-------|---------|
| jugador   | Jugadores, OnlySorti, sortiGO, ModoSorti | atencion + conversion |
| agente    | Agente 👨👩 | satisfaccion + resolucion |
| marketing | Departamento de Makerting | descriptivo |
| interno   | (vacio) | uso interno, no se puntua |
| descartar | Prueba | fuera del analisis |

Ver `src/segments.py` (con tests).

## Grano de evaluacion

La **conversacion**, no el ticket. Un ticket agrupa hasta 533 conversaciones de
meses distintos; puntuar por ticket no tiene sentido.

## Arquitectura de despliegue

Todo vive en EasyPanel, junto al ETL, contra la **misma BD interna**:

```
SERVIDOR EASYPANEL (GPU + Ollama en el host)
  ├ Postgres (compartida con el ETL)
  ├ ETL monitor-sistemas / monitor-datos   (ya corren)
  └ dashboard-whaticket  (UN solo contenedor):
       ├ API + dashboard  -> lee la BD (scopeado por cuenta), sirve el HTML
       └ scoring worker   -> hilo en background (si SCORING_ENABLED),
                             Ollama del host, escribe conversation_scores
```

**Un solo contenedor** sirve el dashboard/API y, si `SCORING_ENABLED=true`,
levanta el worker de scoring en un hilo (segundo plano incremental: puntua las
conversaciones cerradas sin score, por cuenta). El worker llega a Ollama por
`host.docker.internal:11434`. Todo es configurable por entorno (EasyPanel):

| Variable | Default | Que hace |
|---|---|---|
| `DATABASE_URL` | `postgres local` | Postgres compartida con el ETL |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434` / `qwen3.5:4b` | LLM local |
| `API_PORT` | `8080` | Puerto del dashboard/API |
| `SCORING_ENABLED` | `false` | Activa el worker en el contenedor |
| `SCORING_ACCOUNTS` | `sistemas,datos` | Cuentas a scorear (conviven en la misma BD) |
| `SCORING_BATCH_SIZE` | `20` | Conversaciones por lote |
| `SCORING_POLL_SECONDS` | `60` | Espera cuando no hay pendientes |

**Distincion de cuentas**: `datos` y `sistemas` estan en la MISMA base; el
dashboard trae una u otra segun el selector, y el worker scorea las cuentas
configuradas. Desarrollo: contra una **copia local** (snapshot) de la BD.

## Estado

Ver el plan por fases en las tareas del proyecto. Arrancado el scaffold; la
rubrica de estrellas y el schema `conversation_scores` se cierran en la Fase 2,
despues de restaurar la BD (Fase 0) y ordenar el modelo de datos (Fase 1).

## Desarrollo

```bash
pip install -r requirements.txt
pytest                                        # corre los tests

# Dashboard + API en vivo (arranca el worker si SCORING_ENABLED=true)
uvicorn src.app:app --host 0.0.0.0 --port 8080

# Scoring manual (batch) sin levantar la API:
python -m scripts.run_scoring --limit 100 --skip-scored
python -m scripts.run_scoring --limit 20 --diverse   # muestra variada para auditar
```
