# SeedFlip backend container.
#
# Builds the FastAPI app and serves the bundled React frontend from /.
# Expects the prebuilt frontend at `dist/` next to this Dockerfile.
# Wallet keypair and SQLite database persist to /data via the mounted volume.

FROM python:3.12-slim AS builder
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

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY dist ./dist

# Persistent volume target. The runtime mounts /data with the wallet keypair
# and SQLite DB. Path is configurable via WALLET_SECRET_PATH env if you want.
VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
