# syntax=docker/dockerfile:1
#
# One image definition, three targets: api, worker, migrator.
#
# This replaces docker/Dockerfile.api and docker/Dockerfile.worker, which were
# byte-identical apart from their CMD. It exists in this shape because the shared
# deploy pipeline (QNSC-VN/qnsc-ci .github/workflows/backend-deploy.yml) builds each
# service by passing `build-target` against ONE Dockerfile at the repo root; it has no
# per-service dockerfile input. Splitting the file back out means the pipeline can only
# ever build one of the three.
#
# Build locally:
#   docker build --target api      -t qnsc-kb-api .
#   docker build --target worker   -t qnsc-kb-worker .
#   docker build --target migrator -t qnsc-kb-migrator .

# ---------------------------------------------------------------------------
# deps — resolve and install the Python environment once, share it with every
# target. build-essential and libpq-dev stay here and never reach a shipped image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS deps

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.create false && \
    poetry install --no-root --only main

# ---------------------------------------------------------------------------
# runtime — the common layer under all three targets.
#
# Application code is COPYed here rather than per-target so the three images share
# every layer up to this point; ECR then stores one copy of the ~GB dependency
# layers instead of three.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # /app must be importable, not merely the working directory. Running a script BY
    # PATH (`python scripts/bootstrap_db_role.py`) puts /app/scripts on sys.path — not
    # /app — so `import src.core.config` raises ModuleNotFoundError. uvicorn and celery
    # hide this because they import by module name from the CWD, so the failure appears
    # only in the migrator, only when deployed.
    PYTHONPATH=/app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY . .

RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/storage/sources /app/storage/connectors && \
    chown -R appuser:appuser /app

# ---------------------------------------------------------------------------
# model-cache — bake the embedding weights into the image.
#
# src/lib/embeddings.py loads SentenceTransformer(EMBEDDING_MODEL) lazily on first
# use. Without the weights in the image, every cold task downloads ~2.3 GB from
# HuggingFace before it can answer a single query — on Fargate Spot, where tasks are
# replaced without warning, that is a cold start measured in minutes and a hard
# runtime dependency on huggingface.co being reachable and up.
#
# BAKE_EMBEDDING_MODEL defaults to true because the shared build action exposes no
# build-arg input, so CI cannot opt in. Local builds opt OUT:
#   docker build --target api --build-arg BAKE_EMBEDDING_MODEL=false .
#
# EMBEDDING_MODEL must match the runtime setting of the same name. A mismatch is not
# an error — it silently downloads the other model at first use, which is the exact
# cold start this stage exists to remove.
# ---------------------------------------------------------------------------
FROM runtime AS model-cache

ARG BAKE_EMBEDDING_MODEL=true
ARG EMBEDDING_MODEL=BAAI/bge-m3

ENV HF_HOME=/opt/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/huggingface

RUN mkdir -p "$HF_HOME" && \
    if [ "$BAKE_EMBEDDING_MODEL" = "true" ]; then \
        python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"; \
    fi && \
    chown -R appuser:appuser "$HF_HOME"

# ---------------------------------------------------------------------------
# api — FastAPI under uvicorn.
#
# NOTE: this target deliberately has NO entrypoint running Alembic. The previous
# docker/entrypoint.sh ran `alembic upgrade head` before starting the server, which is
# correct for a single-VPS compose and wrong for ECS: every task runs it on boot, so a
# deploy or a scale-out fires N concurrent migrations against one database. Migrations
# belong to the `migrator` target below, which the deploy pipeline runs ONCE, before
# rolling any service.
# ---------------------------------------------------------------------------
FROM model-cache AS api

USER appuser

EXPOSE 8000

# Liveness only — /health/live answers 200 without touching a dependency. Never point
# this at /health/ready: a dependency-coupled probe here restarts the task whenever the
# database or cache blips, turning a hiccup into an outage. Readiness is checked once,
# after the roll, by the deploy pipeline.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')"

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# worker — Celery worker. The same target also serves `celery beat`, which runs as a
# second container off this image with its own command; beat is a singleton and must
# never be scaled past one replica.
# ---------------------------------------------------------------------------
FROM model-cache AS worker

USER appuser

CMD ["celery", "-A", "src.workers.celery_app", "worker", "--loglevel=info", \
     "-Q", "celery,ingestion,connectors,permissions"]

# ---------------------------------------------------------------------------
# migrator — one-shot task: ensure the least-privilege app role exists, then migrate.
#
# Built from `runtime`, not `model-cache`: migrations/env.py imports src.models (for
# target_metadata) and nothing under src/lib/embeddings.py, so this image never loads
# the embedding weights and does not need them baked in.
# ---------------------------------------------------------------------------
FROM runtime AS migrator

USER appuser

ENTRYPOINT ["/app/docker/migrate-entrypoint.sh"]
CMD ["alembic", "-c", "migrations/alembic.ini", "upgrade", "head"]
