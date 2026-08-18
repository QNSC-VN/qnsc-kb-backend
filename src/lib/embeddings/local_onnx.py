"""In-process embeddings on ONNX Runtime — the same model, without torch.

WHY THIS EXISTS. The torch backend is correct and it is what the corpus was built with,
but it costs ~2 GB of framework on top of ~2.3 GB of fp32 weights, in BOTH the api and
worker images, so that the api can embed one short query per search. That drives the
image size, the task memory floor, the registry bill and — on spot capacity, where a task
is replaced without warning — the cold start, repeatedly.

ONNX Runtime executes the identical graph with none of that: `onnxruntime` plus
`tokenizers` and no torch, no transformers. Quantising the exported model to int8 takes
the weights from ~2.3 GB to roughly 600 MB on top.

THE VECTORS MUST MATCH, NOT MERELY WORK. Every chunk already stored was embedded by the
torch backend. A query embedded by a backend that pools differently, or skips
normalisation, lands in a different space and retrieval degrades silently — no error, just
worse answers. So this is not a drop-in until proven: tests/unit/test_embedding_backends.py
asserts cosine similarity ≥ 0.999 against the torch backend for the same input, and that
gate is what decides whether the corpus needs re-embedding.

PRODUCING THE ARTEFACTS. The image bakes the export (Dockerfile `embedding-export`
stage) rather than downloading at boot:

    optimum-cli export onnx --model BAAI/bge-m3 --task feature-extraction /tmp/onnx-fp32
    optimum-cli onnxruntime quantize --onnx_model /tmp/onnx-fp32 --avx2 \
        -o /opt/embedding-onnx

`$EMBEDDING_ONNX_DIR` must hold `model.onnx` and `tokenizer.json`. fp32 on purpose:
both int8 dynamic-quantisation recipes were measured against this gate and lost
(cosine 0.972-0.987 per-tensor and per-channel, with long inputs worse), while fp32
measures 1.000000. The weights stay ~2.3 GB, but torch and its ~700 MB of
site-packages leave the image entirely, which is most of the win.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.core.config import settings
from src.lib.embeddings.base import EmbeddingUnavailable, Lazy

logger = structlog.get_logger()

MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "tokenizer.json"


def _load() -> tuple[Any, Any]:
    directory = Path(settings.EMBEDDING_ONNX_DIR or "")
    if not directory.is_dir():
        raise EmbeddingUnavailable(
            f"EMBEDDING_RUNTIME=onnx but EMBEDDING_ONNX_DIR={str(directory)!r} is not a "
            "directory. The image bakes the export; see this module's docstring."
        )
    model_path, tokenizer_path = directory / MODEL_FILE, directory / TOKENIZER_FILE
    for path in (model_path, tokenizer_path):
        if not path.is_file():
            raise EmbeddingUnavailable(f"{path} is missing from the ONNX export")

    try:
        import onnxruntime
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise EmbeddingUnavailable(
            "EMBEDDING_RUNTIME=onnx needs the optional 'onnx' dependency group "
            "(onnxruntime, tokenizers). Install with `poetry install --with onnx`."
        ) from exc

    logger.info("Loading ONNX embedding model", path=str(model_path))
    options = onnxruntime.SessionOptions()
    # One model, many small requests: the default thread pool oversubscribes a 0.5 vCPU
    # task and spends more time scheduling than embedding.
    options.intra_op_num_threads = settings.EMBEDDING_ONNX_THREADS
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(
        str(model_path), options, providers=["CPUExecutionProvider"]
    )

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=settings.EMBEDDING_MAX_TOKENS)
    tokenizer.enable_padding()
    logger.info("ONNX embedding model ready", inputs=[i.name for i in session.get_inputs()])
    return session, tokenizer


_model = Lazy(_load, "ONNX Runtime")


class OnnxEmbeddingProvider:
    name = "onnx"

    def warm_up(self) -> None:
        _model.get()

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy

        session, tokenizer = _model.get()
        encodings = tokenizer.encode_batch(texts)

        # Feed only what this export actually declares. bge-m3 exports carry
        # token_type_ids; some models do not, and passing an undeclared input is a hard
        # ORT error rather than an ignored extra.
        declared = {i.name for i in session.get_inputs()}
        feed = {
            "input_ids": numpy.array([e.ids for e in encodings], dtype=numpy.int64),
            "attention_mask": numpy.array(
                [e.attention_mask for e in encodings], dtype=numpy.int64
            ),
            "token_type_ids": numpy.array(
                [e.type_ids for e in encodings], dtype=numpy.int64
            ),
        }
        outputs = session.run(None, {k: v for k, v in feed.items() if k in declared})
        return _pool(outputs[0], feed["attention_mask"]).tolist()


def _pool(last_hidden_state: Any, attention_mask: Any) -> Any:
    """Reduce per-token states to one vector per input.

    Pooling is NOT a detail: choosing the wrong one produces perfectly valid vectors in
    the wrong space, so retrieval quietly gets worse and nothing raises. bge-* models are
    trained for CLS pooling — sentence-transformers reads that from the model's own
    config, which an ONNX export does not carry, so it is stated explicitly here and
    verified against the torch backend by the parity test.
    """
    import numpy

    if settings.EMBEDDING_ONNX_POOLING == "cls":
        return last_hidden_state[:, 0]
    if settings.EMBEDDING_ONNX_POOLING == "mean":
        mask = numpy.expand_dims(attention_mask, -1).astype(last_hidden_state.dtype)
        summed = (last_hidden_state * mask).sum(axis=1)
        counts = numpy.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
        return summed / counts
    raise EmbeddingUnavailable(
        f"EMBEDDING_ONNX_POOLING={settings.EMBEDDING_ONNX_POOLING!r} is not one of 'cls', 'mean'"
    )
