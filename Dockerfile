# Образ один для сервиса и генератора — разница только в команде запуска:
#   docker compose service   → python -m vq.service
#   docker compose generator → python -m vq.generator
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

CMD ["python", "-m", "vq.service"]
