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
CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${API_PORT:-8080}"]
