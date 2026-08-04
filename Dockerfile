FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Un solo contenedor: sirve el dashboard + API y, si SCORING_ENABLED=true,
# arranca el worker de scoring en un hilo (ver src/app.py lifespan).
# Config por entorno (EasyPanel): DATABASE_URL, OLLAMA_URL, OLLAMA_MODEL,
# SCORING_ENABLED, SCORING_ACCOUNTS, SCORING_BATCH_SIZE, SCORING_POLL_SECONDS.
#
# El `exec` NO es opcional: sin el, PID 1 queda siendo el `sh` (necesario solo
# para expandir ${API_PORT}). El kernel descarta las senales con accion por
# defecto dirigidas a PID 1, y dash no las reenvia a sus hijos -> el SIGTERM del
# redeploy nunca llega a uvicorn, que muere por SIGKILL al vencer la gracia:
# exit 137, task en rojo en EasyPanel y CERO logs de apagado. Con `exec`,
# uvicorn reemplaza al sh y recibe el SIGTERM -> el lifespan corta el worker de
# scoring y el contenedor sale con 0.
CMD ["sh", "-c", "exec uvicorn src.app:app --host 0.0.0.0 --port ${API_PORT:-8080}"]
