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
#      reaches BOTH api and worker — torch + weights while torch is the default runtime,
#      plus the int8 ONNX export whose parity gate and eventual EMBEDDING_RUNTIME flip
#      live in tests/unit/test_embedding_backends.py — because EMBEDDING_MODEL is a local
#      model and the api embeds the search query on every search. That is the deliberate
#      cost of not sending text to a hosted embedder. The migrator gets none of it.
#
# Build locally (skip the ~2.3 GB torch bake and the ~2.3 GB ONNX copy; the export
# stage itself still builds and caches, since a referenced stage cannot be skipped):
#   docker build --target api  -t qnsc-kb-api . \
#     --build-arg BAKE_EMBEDDING_MODEL=false --build-arg BAKE_EMBEDDING_ONNX=false
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
# deps-ml — the same, plus torch, sentence-transformers, and the ONNX runtime pair.
# api and worker.
#
# Not optional in practice: EMBEDDING_MODEL defaults to BAAI/bge-m3, and
# src/lib/embeddings.py loads it in-process. Without this group the api answers /health
# and then raises on the first search, because the failure is a lazy import inside the
# model singleton rather than anything visible at startup.
#
# The `onnx` group rides along during the transition: the parity test in
# tests/unit/test_embedding_backends.py needs BOTH runtimes importable in the image, and
# EMBEDDING_RUNTIME=onnx needs onnxruntime + tokenizers without torch. Once parity holds
# and the flip is made, `ml` drops out of this line and torch leaves the api image.
# ---------------------------------------------------------------------------
FROM deps AS deps-ml

# `--only main,ml,onnx`, NOT `--only main --with ml`. `--only` is an exhaustive list, so
# mixing `--only` and `--with` silently installs main alone.
RUN poetry install --no-root --only main,ml,onnx

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
RUN poetry install --no-root --only main,ml,ocr,onnx

# ---------------------------------------------------------------------------
# embedding-export — build-only: turns the model into an ONNX export. optimum and its
# exporters never ship, because they are build tools here exactly as build-essential
# is in `deps`, and pinning them in this stage keeps poetry.lock's runtime surface clean.
#
# The stage re-downloads the weights rather than sharing runtime-ml's copy — one stage
# cannot read another's layers mid-build — but Docker layer caching makes that a
# once-per-(model, optimum-version) cost, the same property the torch bake has.
#
# FP32 ON PURPOSE, NOT int8. Both dynamic-quantisation recipes were measured against the
# torch backend (2026-08-17, in the built image) and neither is close to the 0.999
# parity gate: per-tensor --avx2 cosine 0.972-0.981, --per_channel 0.981-0.987 with the
# long input collapsing to 0.908. bge-m3's XLM-R stack does not survive weight
# quantisation at retrieval-grade fidelity, and the gate exists to say no exactly here.
# fp32 measures cosine 1.000000 on every parity case, so it ships: still no torch
# (~700 MB less site-packages), one copy of the weights instead of two, and a
# seconds-long session load instead of a 13 s torch load. int8 stays parked until it can
# be re-evaluated against a full corpus re-embed (EMBEDDING_VERSION bump), which is the
# only regime where its error is self-consistent.
FROM deps-ml AS embedding-export

ARG EMBEDDING_MODEL=BAAI/bge-m3
ENV HF_HOME=/tmp/hf-export

# Exact pin on purpose: a floating optimum re-exports a different graph, which gets a
# new layer digest, and ECR bills that as another full copy of the artefact.
#
# optimum-onnx, not optimum[exporters]: the ONNX exporter moved to its own package when
# optimum 2.x split, and the old extra pins model_patcher code that imports
# `_attention_scale` from torch's legacy symbolic registry — present through torch 2.12,
# gone in the 2.13 this image carries (ImportError at CLI startup, verified in a scratch
# container). optimum-onnx 0.1.0 (optimum 2.1.0) imports clean against torch 2.13. It
# resolves transformers 4.x in this build-only stage; the runtime image keeps 5.15 from
# the lock, and the two never meet — the artefact is the only thing that crosses over.
RUN pip install --no-cache-dir 'optimum-onnx==0.1.0' && \
    optimum-cli export onnx --model "${EMBEDDING_MODEL}" \
        --task feature-extraction /opt/embedding-onnx && \
    python -c "import onnx; m = onnx.load('/opt/embedding-onnx/model.onnx'); \
               print('onnx export ok:', len(m.graph.node), 'nodes')" && \
    rm -rf "$HF_HOME"

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
# runtime-ml — the api's base, carrying torch, sentence-transformers, the model weights,
# and (by default) the int8 ONNX export that EMBEDDING_RUNTIME=onnx would serve.
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
ARG BAKE_EMBEDDING_ONNX=true
ARG EMBEDDING_MODEL=BAAI/bge-m3
# EMBEDDING_TORCH_DIR ships in the image even when BAKE_EMBEDDING_MODEL=false — a
# bake-less local image then logs one warning and falls back to the repo id and the
# developer's own HF cache (see _model_source in local_torch.py), which is the local
# workflow this build-arg exists for.
ENV HF_HOME=/opt/huggingface \
    EMBEDDING_TORCH_DIR=/opt/huggingface/model

# ONE COPY OF THE WEIGHTS. The bge-m3 repo carries its weights ONCE, as
# pytorch_model.bin — but a repo-id load measured 4.35 GB of cache against 2.27 GB of
# weights: the Xet transfer client keeps a chunk cache beside the blobs, and the hub's
# broad download globs (*.json, *.model) also reach into the repo's onnx/ export and
# can grow into whatever else a repo accrues. So the bake downloads an explicit file
# set into a FIXED directory and hands that directory to sentence-transformers, here and
# at run time via EMBEDDING_TORCH_DIR: a directory load matches no hub patterns and
# touches no network, so the one-copy property cannot be lost again at run time either.
# Excluded by the list: the onnx/ export (~2.3 GB) and the colbert/sparse heads, which
# dense retrieval never loads.
#
# The patterns are the sentence-transformers file set for BERT-family checkpoints:
# weights under BOTH names (files absent from a repo are simply not matched — bge-m3
# has only the .bin, MiniLM only the safetensors), tokenizers under every spelling, and
# sentence_bert_config.json, which carries max_seq_length. A repo that needs a file the
# list omits fails the validation load below, at build time, rather than on the first
# search — and that failure is the signal to extend the list, not to widen it
# speculatively.

# THE BAKE MUST BE BYTE-REPRODUCIBLE, and the cleanup lines below are what make it so.
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
# Each line removes a specific source of variance, confirmed by diffing the layer tarballs
# of two `--no-cache` builds. Removing any one of them makes the digest drift again:
#
#   python -B      the interpreter byte-compiles stdlib modules it imports, and the .pyc
#                  files land in /usr/local/**/__pycache__ — OUTSIDE $HF_HOME, so the
#                  normalisation below cannot reach them. Identical content, new mtimes.
#   rm xet         the Xet transfer client writes $HF_HOME/xet/logs/xet_<timestamp>.log.
#                  The filename itself carries the clock. Nothing reads it after the
#                  download, and the chunk cache it sits beside only speeds a re-download
#                  that will never happen in an image.
#   rm .cache      a local_dir download leaves .cache/huggingface metadata (per-file
#                  sha256s, incomplete-part markers) inside the model directory. Nothing
#                  reads it after the bake, and it carries mtimes of its own.
#   touch          content is content-addressed and therefore already identical; mtimes
#                  are not. -h so symlink timestamps are set rather than their targets'.
#                  /opt-wide rather than $HF_HOME-only, because /opt now carries the
#                  ONNX export too, and -depth reaches the directory itself — creating
#                  a directory bumps its parent's mtime, and /opt alone kept the digest
#                  moving after everything else was fixed.
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
#
# The ONNX copy rides on this RUN through a bind mount from the embedding-export stage
# rather than a COPY, because COPY cannot be conditional on a build-arg: with
# BAKE_EMBEDDING_ONNX=false the artefact stays out of the image (the export stage still
# builds and caches — Docker only skips stages nothing references, and this mount
# references it). cp -a carries the export's build-time mtimes in, which is exactly what
# the /opt-wide normalisation below exists to erase.
RUN --mount=type=bind,from=embedding-export,source=/opt/embedding-onnx,target=/mnt/onnx-export \
    mkdir -p "$HF_HOME" /opt/embedding-onnx && \
    if [ "$BAKE_EMBEDDING_MODEL" = "true" ]; then \
        XDG_CACHE_HOME="$HF_HOME/.xdg" TORCH_HOME="$HF_HOME/torch" \
        TORCHINDUCTOR_CACHE_DIR="$HF_HOME/.inductor" \
        python -B -c "from huggingface_hub import snapshot_download; snapshot_download('${EMBEDDING_MODEL}', local_dir='$HF_HOME/model', allow_patterns=['1_Pooling/config.json', 'config.json', 'config_sentence_transformers.json', 'merges.txt', 'model.safetensors', 'modules.json', 'pytorch_model.bin', 'sentence_bert_config.json', 'sentencepiece.bpe.model', 'special_tokens_map.json', 'spiece.model', 'tokenizer.json', 'tokenizer_config.json', 'vocab.txt']); from sentence_transformers import SentenceTransformer; SentenceTransformer('$HF_HOME/model')"; \
    fi && \
    if [ "$BAKE_EMBEDDING_ONNX" = "true" ]; then \
        cp -a /mnt/onnx-export/. /opt/embedding-onnx/; \
    fi && \
    rm -rf "$HF_HOME/xet" "$HF_HOME/.inductor" "$HF_HOME/model/.cache" && \
    find "$HF_HOME" -type d -name '.locks' -prune -exec rm -rf {} + && \
    find /opt -depth -exec touch -h -d @0 {} + && \
    rm -rf /tmp/* /tmp/.[!.]* && touch -d @0 /tmp && \
    chown -R appuser:appuser "$HF_HOME" /opt/embedding-onnx

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
