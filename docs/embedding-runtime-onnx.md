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

1. **The weights are baked twice.** The bge-m3 repo ships both `pytorch_model.bin` and
   `model.safetensors`, and our bake pulls both — 4.4 GB where 2.2 GB would do, in both images.
   This has nothing to do with ONNX and is fixed with an `allow_patterns` argument on the download.

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

1. **Fix the duplicate weights** with `allow_patterns` — independent of ONNX, ~2 GB off both images,
   near-zero risk. Worth doing on its own even if ONNX is never adopted.
2. Add a Dockerfile stage that exports the model and int8-quantises it, and install the `onnx` group:
   `optimum-cli export onnx --model BAAI/bge-m3 --task feature-extraction $EMBEDDING_ONNX_DIR`
   (the directory needs `model.onnx` and `tokenizer.json`).
3. **Run the parity test in that image.** This is the decision point.
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
