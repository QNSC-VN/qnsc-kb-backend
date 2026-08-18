"""In-process embeddings via sentence-transformers — the reference implementation.

This is what the stored corpus was embedded with, so it defines the vector space every
other backend has to match. Keep it as the default until the ONNX parity test has been
run against the model actually in use.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from src.core.config import settings
from src.lib.embeddings.base import EmbeddingUnavailable, Lazy

logger = structlog.get_logger()


def _model_source() -> str:
    """What SentenceTransformer is handed: the baked snapshot dir, or the repo id.

    Loading by repo id resolves through the hub — broad download globs and the Xet chunk
    cache can hold several copies of what one load needs — and can go to the network
    from a serving task. A directory is exact: what was baked is what loads. The
    directory must actually exist; a stale setting falls back to the repo id rather than
    failing, because a bake-less image behaves like a local checkout and that path must
    keep working.
    """
    if settings.EMBEDDING_TORCH_DIR and os.path.isdir(settings.EMBEDDING_TORCH_DIR):
        return settings.EMBEDDING_TORCH_DIR
    if settings.EMBEDDING_TORCH_DIR:
        logger.warning(
            "EMBEDDING_TORCH_DIR is set but not a directory; falling back to repo id",
            embedding_torch_dir=settings.EMBEDDING_TORCH_DIR,
        )
    return settings.EMBEDDING_MODEL


def _load() -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingUnavailable(
            f"EMBEDDING_MODEL={settings.EMBEDDING_MODEL!r} is loaded in-process and needs "
            "the optional 'ml' dependency group (torch, sentence-transformers). Install "
            "with `poetry install --with ml`, or set EMBEDDING_RUNTIME=onnx."
        ) from exc

    source = _model_source()
    logger.info("Loading SentenceTransformer model", model=source)
    model = SentenceTransformer(source)
    logger.info("SentenceTransformer model ready", model=source)
    return model


_model = Lazy(_load, "sentence-transformers")


class TorchEmbeddingProvider:
    name = "torch"

    def warm_up(self) -> None:
        _model.get()

    def embed(self, texts: list[str]) -> list[list[float]]:
        # normalize_embeddings stays on: it is how this backend has always behaved, and
        # the seam's own normalisation is then a no-op rather than a second opinion.
        vectors = _model.get().encode(
            texts, normalize_embeddings=True, batch_size=settings.EMBEDDING_BATCH_SIZE
        )
        return [vector.tolist() for vector in vectors]
