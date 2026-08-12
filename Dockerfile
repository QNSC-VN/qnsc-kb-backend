# syntax=docker/dockerfile:1
#
# One image definition, three targets: api, worker, migrator.
#
# It lives at the repo root and takes a target because the shared deploy pipeline
# (QNSC-VN/qnsc-ci .github/workflows/backend-deploy.yml) builds each service by passing
# `build-target` against ONE Dockerfile and has no per-service dockerfile input.
#
# TWO RULES SHAPE THIS FILE, both learned the expensive way:
#
#   1. Application code is copied LAST, per target. Docker invalidates every layer above
#      a changed one, so anything large must sit below the thing that changes on every
#      commit. `COPY . .` used to sit under the model bake, so each commit stored a fresh
#      2.3 GB layer — 81.6 GB of ECR across 21 builds.
#
#   2. Each target carries only what it runs. paddle is ~1 GB and only the worker extracts
#      text from scanned files, so the OCR stack stops at the worker. The embedding stack
#      (torch + weights) reaches BOTH api and worker, because EMBEDDING_MODEL is a local
#      model and the api embeds the search query on every search — that is the deliberate
#      cost of not sending text to a hosted embedder. The migrator gets neither.
#
# Build locally:
#   docker build --target api      -t qnsc-kb-api .
#   docker build --target worker   -t qnsc-kb-worker .
#   docker build --target migrator -t qnsc-kb-migrator .

# ---------------------------------------------------------------------------
# deps — the runtime dependency set every target shares. build-essential and libpq-dev
# stay here and never reach a shipped image.
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
# deps-ml — the same, plus torch and sentence-transformers. api and worker.
#
# Not optional in practice: EMBEDDING_MODEL defaults to BAAI/bge-m3, and
# src/lib/embeddings.py loads it in-process. Without this group the api answers /health
# and then raises on the first search, because the failure is a lazy import inside the
# model singleton rather than anything visible at startup.
# ---------------------------------------------------------------------------
FROM deps AS deps-ml

# `--only main,ml`, NOT `--only main --with ml`. `--only` is an exhaustive list, so
# combining the two silently installs main alone.
RUN poetry install --no-root --only main,ml

# ---------------------------------------------------------------------------
# deps-ml-ocr — the same again, plus the OCR stack. Worker only.
#
# src/domain/source_extraction.py imports paddle INSIDE the functions that use it, so an
# image without it serves every other path normally and fails loudly only if asked to
# OCR — which the api never is.
# ---------------------------------------------------------------------------
FROM deps-ml AS deps-ml-ocr

# Same rule as above, and the list must name EVERY group the worker needs, not just the
# one being added: `--only main,ocr` here resolves without ml, so the worker would ship
# paddle and no torch and fail on the first chunk it tried to embed.
RUN poetry install --no-root --only main,ml,ocr

# ---------------------------------------------------------------------------
# runtime — common base. NO application code: see rule 1 above.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # /app must be importable, not merely the working directory. Running a script BY
    # PATH (`python scripts/bootstrap_db_role.py`) puts /app/scripts on sys.path — not
    # /app — so `import src.core.config` raises ModuleNotFoundError. uvicorn and celery
    # hide this because they import by module name from the CWD, so the failure appeared
    # only in the migrator, only once deployed.
    PYTHONPATH=/app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/storage/sources /app/storage/connectors && \
    chown -R appuser:appuser /app

# ---------------------------------------------------------------------------
# runtime-ml — the api's base, carrying torch, sentence-transformers and (by default) the
# model weights themselves.
#
# BAKING THE WEIGHTS IS THE POINT. sentence-transformers downloads on first use, so an
# unbaked image pays ~2.3 GB and several minutes on the first search AFTER the task is
# already serving traffic — repeatedly, on every replacement. Baked, it is paid once at
# build time. Local builds pass BAKE_EMBEDDING_MODEL=false and use the developer's own
# cache instead of storing another copy per rebuild.
#
# HF_HOME is set for BOTH build and run so the two agree on where the weights are; a
# mismatch silently re-downloads at runtime and looks like the bake never happened. It
# sits under /opt rather than the home directory because the deploy may run this as a
# different uid.
#
# This layer is above every `COPY . .` on purpose — see rule 1.
# ---------------------------------------------------------------------------
FROM runtime AS runtime-ml

COPY --from=deps-ml /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps-ml /usr/local/bin /usr/local/bin

ARG BAKE_EMBEDDING_MODEL=true
ARG EMBEDDING_MODEL=BAAI/bge-m3
ENV HF_HOME=/opt/huggingface

# THE BAKE MUST BE BYTE-REPRODUCIBLE, and the four cleanup lines below are what make it so.
#
# Rule 1 keeps this layer off the per-commit path, but it is still rebuilt whenever the
# build cache misses — and it misses constantly, because `type=gha` caps at 10 GB per
# repository while this layer alone is 2.5 GB in each of the `api` and `worker` scopes.
# A rebuilt layer whose bytes differ by even one mtime gets a NEW digest, and ECR bills
# that as another full copy. Measured on qnsc-kb-api: nine distinct 2.53 GB layers, every
# one `shared_by=1` — 22.7 GB of the repository's 24.0 GB was the same weights over again.
#
# Reproducible, the digest is identical on every rebuild, so ECR stores ONE copy no matter
# how often the cache misses. That is a storage fix, not a build-time fix; caching is what
# saves the download, and it is configured in qnsc-ci's build-push-ecr action.
#
# Each line removes a specific source of variance, all four confirmed by diffing the layer
# tarballs of two `--no-cache` builds. Removing any one of them makes the digest drift again:
#
#   python -B      the interpreter byte-compiles stdlib modules it imports, and the .pyc
#                  files land in /usr/local/**/__pycache__ — OUTSIDE $HF_HOME, so the
#                  normalisation below cannot reach them. Identical content, new mtimes.
#   rm xet         the Xet transfer client writes $HF_HOME/xet/logs/xet_<timestamp>.log.
#                  The filename itself carries the clock. Nothing reads it after the
#                  download, and the chunk cache it sits beside only speeds a re-download
#                  that will never happen in an image.
#   touch          content is content-addressed and therefore already identical; mtimes
#                  are not. -h so symlink timestamps are set rather than their targets',
#                  and $HF_HOME's PARENT too — creating a directory bumps its parent's
#                  mtime, and /opt alone kept the digest moving after everything else was
#                  fixed.
#   XDG/TORCH_*   scoped to this command, not ENV: they pull stray library caches under
#                  $HF_HOME so they are normalised with everything else instead of
#                  becoming the next source of drift. TORCHINDUCTOR_CACHE_DIR earns its
#                  place — importing torch creates /tmp/torchinductor_root, and with the
#                  small MiniLM model used to develop this recipe it never appeared. Only
#                  a full bge-m3 build showed it, as the last two differing entries.
#   /tmp           emptied and pinned anyway: redirecting the caches we know about is not
#                  a guarantee about the ones we do not, and /tmp is the conventional
#                  place for them. Nothing written there during a build is needed at run
#                  time, so clearing it costs nothing and removes the whole category.
#
# Verify after changing anything here — build the target twice with --no-cache and compare
# `docker buildx build --output type=oci`; the manifest's last layer digest must match.
RUN mkdir -p "$HF_HOME" && \
    if [ "$BAKE_EMBEDDING_MODEL" = "true" ]; then \
        XDG_CACHE_HOME="$HF_HOME/.xdg" TORCH_HOME="$HF_HOME/torch" \
        TORCHINDUCTOR_CACHE_DIR="$HF_HOME/.inductor" \
        python -B -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"; \
    fi && \
    rm -rf "$HF_HOME/xet" "$HF_HOME/.inductor" && \
    find "$HF_HOME" -type d -name '.locks' -prune -exec rm -rf {} + && \
    find "$HF_HOME" -depth -exec touch -h -d @0 {} + && \
    touch -h -d @0 "$(dirname "$HF_HOME")" && \
    rm -rf /tmp/* /tmp/.[!.]* && touch -d @0 /tmp && \
    chown -R appuser:appuser "$HF_HOME"

# ---------------------------------------------------------------------------
# runtime-ml-ocr — the worker's base: the above, plus paddle.
# ---------------------------------------------------------------------------
FROM runtime-ml AS runtime-ml-ocr

COPY --from=deps-ml-ocr /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps-ml-ocr /usr/local/bin /usr/local/bin

# ---------------------------------------------------------------------------
# api — FastAPI under uvicorn.
#
# Carries the embedding stack but NOT OCR: main.py preloads the model at startup so no
# request pays for loading it, and the api never extracts text from a scanned file.
#
# Deliberately NO entrypoint running Alembic. That is right for a single-VPS compose and
# wrong for ECS, where every task would run it — a deploy or a scale-out firing N
# concurrent migrations against one database. Migrations belong to the `migrator` target,
# which the pipeline runs once, before rolling any service.
# ---------------------------------------------------------------------------
FROM runtime-ml AS api

COPY --chown=appuser:appuser . .

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
# worker — Celery worker, and the image `celery beat` runs from with its own command.
# Beat is a singleton and must never be scaled past one replica.
# ---------------------------------------------------------------------------
FROM runtime-ml-ocr AS worker

COPY --chown=appuser:appuser . .

USER appuser

CMD ["celery", "-A", "src.workers.celery_app", "worker", "--loglevel=info", \
     "-Q", "celery,ingestion,connectors,permissions"]

# ---------------------------------------------------------------------------
# migrator — one-shot: ensure the least-privilege app role exists, then migrate.
#
# Built from `runtime`: migrations/env.py imports src.models for target_metadata and
# nothing that touches embeddings or OCR.
# ---------------------------------------------------------------------------
FROM runtime AS migrator

COPY --chown=appuser:appuser . .

USER appuser

ENTRYPOINT ["/app/docker/migrate-entrypoint.sh"]
CMD ["alembic", "-c", "migrations/alembic.ini", "upgrade", "head"]
