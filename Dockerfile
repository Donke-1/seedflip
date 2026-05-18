# SeedFlip backend container.
#
# Three-stage build:
#   1) frontend-builder (node): npm install + vite build → dist/
#   2) py-builder (python+poetry): install backend deps into a venv
#   3) runtime: copy venv + app + dist into a slim python image
#
# Wallet keypair and SQLite database persist to /data via the mounted volume.

FROM node:20-slim AS frontend-builder
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Build outputs to ../dist relative to /fe (i.e. /dist), per vite.config.ts.
RUN npm run build

FROM python:3.12-slim AS py-builder
WORKDIR /app
RUN pip install --no-cache-dir poetry==1.8.3 \
    && poetry config virtualenvs.in-project true
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only main

FROM python:3.12-slim
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Postgres client libs were a previous dep; not strictly needed for sqlite but
# keep them for psycopg's binary compat in case the user switches stores.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=py-builder /app/.venv /app/.venv
COPY app ./app
COPY --from=frontend-builder /dist ./dist

# Persistent volume target. The runtime mounts /data with the wallet keypair
# and SQLite DB. Path is configurable via WALLET_SECRET_PATH env if you want.
VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
