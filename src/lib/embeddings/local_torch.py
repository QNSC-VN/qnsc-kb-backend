"""In-process embeddings via sentence-transformers — the reference implementation.

This is what the stored corpus was embedded with, so it defines the vector space every
other backend has to match. Keep it as the default until the ONNX parity test has been
run against the model actually in use.
"""
from __future__ import annotations

from typing import Any

import structlog

from src.core.config import settings
from src.lib.embeddings.base import EmbeddingUnavailable, Lazy

logger = structlog.get_logger()


def _load() -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingUnavailable(
            f"EMBEDDING_MODEL={settings.EMBEDDING_MODEL!r} is loaded in-process and needs "
            "the optional 'ml' dependency group (torch, sentence-transformers). Install "
            "with `poetry install --with ml`, or set EMBEDDING_RUNTIME=onnx."
        ) from exc

    logger.info("Loading SentenceTransformer model", model=settings.EMBEDDING_MODEL)
    model = SentenceTransformer(settings.EMBEDDING_MODEL)
    logger.info("SentenceTransformer model ready", model=settings.EMBEDDING_MODEL)
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
