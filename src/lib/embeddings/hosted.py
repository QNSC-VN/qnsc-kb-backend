"""Embeddings from an HTTP API (Gemini's batchEmbedContents shape).

Not the default: document and query text leaves the deployment, which is the property the
in-process backends exist to keep. Kept working because it is the fallback when a
deployment cannot afford to carry a model at all, and because it is what the hosted
option costs in code — a few dozen lines — if that trade is ever re-made.
"""
from __future__ import annotations

import httpx

from src.core.config import settings
from src.lib.embeddings.base import EmbeddingUnavailable


class HostedEmbeddingProvider:
    name = "hosted"

    def warm_up(self) -> None:
        """Nothing to load — the model lives on the other side of the wire."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not settings.GEMINI_API_KEY:
            raise EmbeddingUnavailable(
                "GEMINI_API_KEY is not set, and EMBEDDING_MODEL "
                f"({settings.EMBEDDING_MODEL!r}) is a hosted model."
            )

        model = settings.EMBEDDING_MODEL
        url = f"{settings.GEMINI_API_BASE_URL.rstrip('/')}/models/{model}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                    # Explicit, because the two are NOT interchangeable: the provider
                    # embeds a question and a passage differently, and mixing them
                    # degrades retrieval quietly rather than visibly.
                    "taskType": "RETRIEVAL_QUERY",
                    # gemini-embedding-001 returns 3072 dimensions by default and
                    # pgvector's HNSW index refuses to build above 2000. Ask for the
                    # width the column was actually created with.
                    "outputDimensionality": settings.EMBEDDING_DIMENSION,
                }
                for text in texts
            ]
        }

        response = httpx.post(
            url,
            params={"key": settings.GEMINI_API_KEY},
            json=payload,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        # Truncated vectors come back UNNORMALISED — measured L2 of 0.586 at 768
        # dimensions, where the full-width output is unit length. The seam normalises
        # everything, so that is handled there rather than here.
        return [item["values"] for item in response.json().get("embeddings", [])]
