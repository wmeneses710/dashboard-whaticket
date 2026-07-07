#!/usr/bin/env bash
# Levanta el stack local igual que el contenedor de prod: la MISMA app sirve el
# dashboard y corre el worker de scoring continuo (SCORING_ENABLED=true).
#
# Guarda de memoria: vigila la RAM disponible del sistema (incluye Ollama) y si
# baja del piso (MEM_FLOOR_MB) mata la app para que la maquina no se pete.
#
# Uso:   bash scripts/run_local.sh
#        RESET=1 bash scripts/run_local.sh        # trunca conversation_scores (scorea de cero, como prod)
#        MEM_FLOOR_MB=1536 bash scripts/run_local.sh
set -euo pipefail

# --- Config (override por entorno) ---
DB_CONTAINER="${DB_CONTAINER:-etlwhaticket-db-1}"
PORT="${API_PORT:-8080}"
MEM_FLOOR_MB="${MEM_FLOOR_MB:-1024}"          # corta si RAM disponible < esto (9GB libres -> corta a ~8GB usados)
export DATABASE_URL="${DATABASE_URL:-postgresql://whaticket:whaticket@localhost:5432/whaticket}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:4b}"
export SCORING_ENABLED=true
export SCORING_ACCOUNTS="${SCORING_ACCOUNTS:-sistemas,datos}"
export SCORING_BATCH_SIZE="${SCORING_BATCH_SIZE:-20}"
export API_PORT="$PORT"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> 1) Base de datos ($DB_CONTAINER)"
docker start "$DB_CONTAINER" >/dev/null 2>&1 || true
for i in $(seq 1 20); do
  docker exec "$DB_CONTAINER" pg_isready -U whaticket >/dev/null 2>&1 && { echo "    lista"; break; }
  sleep 1
done

if [ "${RESET:-0}" = "1" ]; then
  echo "==> RESET: truncando conversation_scores (se scorea de cero)"
  docker exec "$DB_CONTAINER" psql -U whaticket -d whaticket -c "TRUNCATE conversation_scores;" >/dev/null
fi

echo "==> 2) Nombres de operador + backfill de depositos (rapido, sin Ollama)"
.venv/bin/python -m scripts.seed_users >/dev/null 2>&1 || true
.venv/bin/python -m scripts.backfill_deposits || true

echo "==> 3) Ollama ($OLLAMA_URL / $OLLAMA_MODEL)"
if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then echo "    alcanzable"; else
  echo "    OJO: Ollama no responde -> el scoring fallara cada ciclo (el dashboard igual sirve)"; fi

# Libera el puerto si quedo algo escuchando
if command -v fuser >/dev/null 2>&1; then fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true; fi

echo "==> 4) App (dashboard + score continuo) en http://127.0.0.1:$PORT"
.venv/bin/uvicorn src.app:app --host 127.0.0.1 --port "$PORT" --log-level info &
APP_PID=$!
trap 'echo; echo "==> parando (pid $APP_PID)"; kill "$APP_PID" 2>/dev/null || true; exit 0' INT TERM

echo "==> Guarda de memoria activa: corto si RAM disponible < ${MEM_FLOOR_MB} MB"
while kill -0 "$APP_PID" 2>/dev/null; do
  avail_kb="$(awk '/MemAvailable/{print $2}' /proc/meminfo)"
  avail_mb=$(( avail_kb / 1024 ))
  if [ "$avail_mb" -lt "$MEM_FLOOR_MB" ]; then
    echo "!! RAM disponible ${avail_mb} MB < ${MEM_FLOOR_MB} MB -> CORTANDO la app para no petar la maquina"
    kill "$APP_PID" 2>/dev/null || true
    break
  fi
  sleep 3
done
wait "$APP_PID" 2>/dev/null || true
echo "==> detenido"
