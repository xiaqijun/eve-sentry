FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY esi_gateway/ ./esi_gateway/
COPY scripts/esi_gateway.py ./scripts/esi_gateway.py
COPY deploy/docker/healthcheck.py ./healthcheck.py

USER 65532:65532
CMD ["python", "scripts/esi_gateway.py", "--transport-mode", "relay"]
