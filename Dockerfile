FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# El entrypoint real (API) se define en la Fase 3.
CMD ["python", "-c", "print('dashboard-whaticket: API pendiente (Fase 3)')"]
