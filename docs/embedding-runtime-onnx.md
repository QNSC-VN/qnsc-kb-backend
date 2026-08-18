# Embeddings: move from PyTorch to ONNX Runtime

All numbers below were measured in the running `develop` environment, not taken from documentation.

## What we run today

The API and the worker both load `BAAI/bge-m3` in-process through `sentence-transformers` on PyTorch.

Measured inside the deployed api image:

```
site-packages                1,471 MB
  torch                        695 MB     ← largest single dependency
  scipy                         83 MB
  transformers                  53 MB
baked model cache            4,354 MB     ← 4.25 GB
  2.2 GB blob  ┐ the same weights stored TWICE:
  2.2 GB blob  ┘ pytorch_model.bin and model.safetensors
```

Consequences:

```
api image      3.24 GB          worker image   3.44 GB
api task       512 / 4096 MB    worker task    1024 / 6144 MB
model load     13 s             task start     3 m 12 s (image pull)
ECR            109 GB now, ~50 GB steady state
```

The api task holds 4 GB **only** to keep the model resident, and it uses it to embed one short
search query per request. Before that memory was raised the load failed and search fell back to
keyword-only — no error, just worse answers.

## Two separate problems

1. ~~**The weights are baked twice.**~~ — corrected and FIXED 2026-08-17. The bge-m3 repo
   does **not** ship `model.safetensors` (checked against the hub file list); its weights are
   `pytorch_model.bin` only. The measured second ~2 GB copy was the Xet transfer chunk cache
   sitting beside the blobs. The fix is still the one sketched here, plus one thing it turned
   out to need: the bake downloads an explicit file list into `/opt/huggingface/model`, and the
   runtime loads **that directory** (`EMBEDDING_TORCH_DIR` in `src/core/config.py`), because
   loading by repo id re-runs the hub's broad download globs and could re-grow the cache from a
   serving task. Also excluded by the list: the repo's `onnx/` export (~2.3 GB) and the
   colbert/sparse heads. Expected: 4.35 GB → 2.3 GB of model cache in both images.

2. **torch costs 695 MB for a forward pass.** It is a training framework; we only ever run inference.

## What ONNX changes

Same model, same vectors, different execution engine.

- torch is no longer installed — `onnxruntime` + `tokenizers` replace it
- int8 quantisation takes the weights from 2.2 GB to roughly 600 MB
- CPU inference is typically faster than torch for this workload
- resident memory roughly halves, so plausibly **api 4096 → 2048** and **worker 6144 → 3072**
- cold start drops from minutes to seconds

With the duplicate-weight fix as well, images plausibly go from 3.4 GB to under 1 GB.

**Privacy is unchanged.** The model still runs inside our VPC; no document or query text leaves the
deployment. That property is why local embeddings were chosen, and it is preserved exactly.

## The risk, and the gate

A different runtime must produce the *same* vectors, or every stored chunk becomes invalid. This
would not raise an error — a query embedded into a slightly different space simply returns worse
results. Two specific hazards:

- **Pooling.** bge-m3 is CLS-pooled. `sentence-transformers` reads that from the model config; an
  ONNX export does not carry it, so it must be stated explicitly.
- **Normalisation.** The tuned thresholds (`VECTOR_DISTANCE_THRESHOLD`, `RAG_MIN_RELEVANCE_SCORE`)
  assume unit-length vectors.
- **Truncation.** Found 2026-08-17 during audit, after the doc was written:
  `sentence-transformers` truncates at the model's own `max_seq_length` (8192 for bge-m3), while
  the ONNX tokenizer truncated at `EMBEDDING_MAX_TOKENS=512`. Every original parity input sat
  under 512 tokens, so the gate was blind to the divergence — and real chunks (~1500 chars of
  Vietnamese ≈ 700-1000 tokens) are over it. The parity test now carries a >512-token case, and
  `EMBEDDING_MAX_TOKENS` moved to 8192 to match the torch side. The corpus compatibility rule
  decided which side moves: the stored vectors were computed full-length.

The gate is already in the repo. `tests/unit/test_embedding_backends.py` asserts **cosine similarity
≥ 0.999** between the torch and ONNX backends on the same input, including a Vietnamese case. It
currently skips because no export is baked. If it passes, switching runtime is a config change. If it
fails, the corpus must be re-embedded — and that is the decision point, not a detail.

## Already done

PR #23 (merged) separated the two concerns:

- `EMBEDDING_MODEL` — which vectors (changing it invalidates every stored chunk)
- `EMBEDDING_RUNTIME` — how they are computed (`torch` | `onnx`)

The ONNX provider is written, the width/normalisation invariants are enforced in one place, and
`onnxruntime 1.28.0` + `tokenizers 0.22.2` are locked in an optional `onnx` dependency group.
Default remains `torch`, so nothing has changed in behaviour yet.

## The ask

1. ~~**Fix the duplicate weights** with `allow_patterns`~~ — DONE 2026-08-17; see the corrected
   diagnosis under "Two separate problems". Independent of ONNX, ~2 GB off both images,
   near-zero risk.
2. ~~Add a Dockerfile stage that exports the model and int8-quantises it, and install the `onnx`
   group~~ — DONE 2026-08-17. The `embedding-export` build stage (build-only, `optimum[exporters]`
   pinned at 1.27.0, never shipped) exports fp32 then quantises to int8 with `--avx2` — Fargate's
   x86 fleet cannot be relied on for AVX-512/VNNI. The artefact lands in `/opt/embedding-onnx`
   behind `BAKE_EMBEDDING_ONNX`, with the same mtime-normalisation discipline as the torch bake
   so the layer stays byte-reproducible. `onnx` group added to both the api and worker dependency
   stages; `ml` stays until the flip (the parity test needs both runtimes in the image).
3. ~~**Run the parity test in that image.**~~ — RUN 2026-08-17 and **PASSED: 14/14**, cosine ≥ 0.999
   on every case including the long-input one. The decision it guards resolved like this: **int8 was
   rejected by measurement, fp32 ships.** Both dynamic-quantisation recipes were compared against
   the torch backend inside the built image — per-tensor `--avx2` cosine 0.972-0.981,
   `--per_channel` 0.981-0.987 with the long input collapsing to 0.908 — while the fp32 export
   measures **1.000000** on every case. bge-m3's XLM-R stack does not survive weight quantisation
   at retrieval-grade fidelity. Consequences: the artefact stays ~2.2 GB (not ~600 MB), so the
   image lands near 2.7 GB rather than under 1 GB and the api floor is ~2048 MB rather than
   1024 — but torch, transformers and scipy still leave the image entirely, the weights exist
   once instead of twice, and the model loads in ~3 s instead of ~13 s. int8 stays parked until
   a full corpus re-embed (an `EMBEDDING_VERSION` bump) makes its error self-consistent.
   Measured in the image (2026-08-17): onnx warm-up 3.2 s / peak RSS 1.53 GB / query 116 ms;
   torch warm-up 8.4 s / peak RSS 2.03 GB. The torch path still loads from the baked snapshot,
   so `EMBEDDING_RUNTIME=torch` remains a working rollback until the `ml` group is dropped.
4. If parity holds: set `EMBEDDING_RUNTIME=onnx`, drop the `ml` group from the api image, re-measure
   RSS, and right-size both tasks.

Expect two or three build iterations; each is roughly 25 minutes.

## Why it is worth it

- roughly **$13/month** off develop (Fargate + ECR), and more in production, which has no idle
  schedule and therefore pays around the clock
- **cold start from minutes to seconds** — the user-visible one. On Fargate Spot a task can be
  replaced at any moment, and today that costs a 3-minute image pull plus a 13-second model load
  before the first query is answered
- the api task stops being sized by a model it uses for one short string per search

---

# ~~Unrelated to ONNX: `connectors.sync_interval_minutes` does not exist~~ — FIXED 2026-08-17

**Resolved before handover; kept as the record.** The attribute was removed from
`src/models/ops.py`, which is the direction the evidence pointed — see "The fix" below.
Nothing in "The ask" changed, and no migration was added.

Filed here originally because it landed on the same person and in the same pass, **not**
because it is part of the migration above.

## The symptom

A Celery task fails every ten minutes in `develop`. Measured in
`/ecs/qnsc-kb-develop-worker`, 8 occurrences in two hours on 2026-08-17:

```
[error] Celery task failed
  error=(sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError)
  <class 'asyncpg.exceptions.UndefinedColumnError'>:
  column connectors.sync_interval_minutes does not exist
```

`/health/ready` still returns `200` with `database: ok` — the connection is fine, so nothing about
the health check or the deploy reports this. It is only visible in the worker log.

## The cause

`src/models/ops.py:19` declares a column that no migration creates:

```python
sync_interval_minutes: Mapped[int] = mapped_column(
    Integer, nullable=False, default=60, server_default="60"
)
```

There is no `sync_interval` anywhere in `migrations/versions/` — checked against every revision up
to `20260810_51_realign_embedding_dimension`. SQLAlchemy therefore puts the column in the `SELECT`
list for any query loading `Connector`, and Postgres rejects the statement.

## The fix is to DELETE it, not to add a migration

`grep -rn sync_interval` over the whole repository returns **one** line: the declaration above.
Nothing reads it, nothing writes it, and no API exposes it. The model's own comment already says so:

> Retained for compatibility with pre-Alembic connector rows. Current scheduling uses connector
> config and job mode; no API exposes this field.

So a migration would add a column purely to satisfy a mapping nobody uses, and the drift would be
resolved in the direction that costs a schema change and keeps dead state. Removing the attribute
resolves it in the direction that deletes dead code.

If it turns out something *does* depend on it, the migration is
`op.add_column("connectors", sa.Column("sync_interval_minutes", sa.Integer(), nullable=False,
server_default="60"))` — the `server_default` in the model means existing rows backfill without a
data migration. But establish the consumer first.

## Why it matters before production

kb production does not exist yet, so this is currently a develop-only annoyance. It would not stay
one: the same model runs everywhere, so the first production connector query fails the same way,
and there it fails without a develop log anybody is already watching.

## Verifying the fix

Done as described. Confirm the ten-minute failure has stopped:

```bash
aws logs filter-log-events --region ap-southeast-1 \
  --log-group-name /ecs/qnsc-kb-develop-worker \
  --start-time $(python3 -c 'import time;print(int(time.time()*1000)-1800000)') \
  --filter-pattern 'sync_interval_minutes' --query 'length(events)'
```

Expect `0` across a window longer than the beat interval. **A quiet window shorter than that proves
nothing** — the failure only appears when the scheduled task actually fires, and it went quiet for
40 minutes on 2026-08-17 without being fixed, which is what made it easy to miss.
