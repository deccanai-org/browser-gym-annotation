FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# The docker CLI, for workspace isolation: each attempt's gym runs as its own
# container (app/workspace/provider.py DockerRuntimeProvider). The backend image
# holds neither the gym source nor Playwright, so the process provider cannot
# work from in here. Needs /var/run/docker.sock mounted to be usable.
# docker-cli, NOT docker.io: with --no-install-recommends the latter installs the
# daemon and leaves only docker-init, no client binary at all (measured).
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# deps first for layer caching
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Cloud Run injects $PORT (8080); default 8090 for local docker-compose.
# Set RUN_MIGRATIONS=1 (prod) to apply Alembic migrations before serving.
EXPOSE 8090
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"1\" ]; then alembic upgrade head; fi; uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8090}"]
