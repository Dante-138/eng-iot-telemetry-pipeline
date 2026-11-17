# One image for the Python parts: ingest service, simulator, provisioning and
# approval report. Pure logic lives in telemetry/.
FROM python:3.12.16-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app

COPY requirements.txt requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY telemetry ./telemetry
COPY simulator ./simulator
COPY ingest ./ingest
COPY gateway_tap ./gateway_tap
COPY provision ./provision
COPY approve ./approve
COPY sql ./sql
COPY config ./config

CMD ["python", "-m", "ingest.service"]
