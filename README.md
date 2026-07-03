# dashboard-whaticket

Evaluacion de calidad de atencion y dashboard sobre las conversaciones que
captura el ETL de Whaticket (proyecto hermano `ETLWhaticket`).

No re-captura datos: **lee la misma BD Postgres** que llena el ETL, puntua cada
conversacion con un LLM local (Ollama) y sirve un dashboard HTML interactivo.

## Que hace

1. **Metricas objetivas** (SQL, sin LLM): tiempo a primera respuesta, tiempo de
   resolucion, nº de mensajes, reaperturas, conversaciones sin asignar.
2. **Scoring semantico** (LLM local `qwen3.5:4b`): forma, contexto, tono, si
   entendio, si empujo, si resolvio.
3. **Calificacion por estrellas (1-5)**: combinacion ponderada de (1) + (2).
   Es una **ESTIMACION**, no un dato de la plataforma (Whaticket trae `csat`
   vacio). El dashboard lo marca como tal.
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
  ├ scoring worker   -> Ollama del host, escribe conversation_scores
  └ API + dashboard  -> lee la BD, sirve el HTML
```

El scoring corre en **segundo plano incremental** (puntua conversaciones
cerradas sin score) + un **backfill** inicial para el historico. No en tiempo
real. El worker llega a Ollama del host por `host.docker.internal:11434`.

Desarrollo: contra una **copia local** (snapshot) de la BD. El codigo se
despliega; los datos no viajan.

## Estado

Ver el plan por fases en las tareas del proyecto. Arrancado el scaffold; la
rubrica de estrellas y el schema `conversation_scores` se cierran en la Fase 2,
despues de restaurar la BD (Fase 0) y ordenar el modelo de datos (Fase 1).

## Desarrollo

```bash
pip install -r requirements.txt
pytest                # corre los tests
```
