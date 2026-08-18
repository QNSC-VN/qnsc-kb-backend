"""The rules every embedding backend obeys, and the gate the ONNX swap must pass.

Two kinds of test here:

  INVARIANTS — width, unit length, no zero vectors, honest failures. They run against a
  fake backend, so they hold for any runtime and need no weights.

  PARITY — cosine similarity between the torch and ONNX backends for identical input.
  Skipped unless BOTH are installed and an export is present, because that is the only
  situation where the question is meaningful. This is the test that decides whether
  switching EMBEDDING_RUNTIME is a config change or a re-embed of the whole corpus: the
  stored chunks were produced by torch, and a query embedded into a different space
  degrades retrieval silently rather than failing.
"""
from __future__ import annotations

import math
import os

import pytest

from src.core.config import settings
from src.lib import embeddings
from src.lib.embeddings import base, local_torch


class FakeProvider:
    """Returns whatever it is told to, so the seam's own rules can be tested."""

    name = "fake"

    def __init__(self, vectors):
        self._vectors = vectors

    def warm_up(self) -> None:
        pass

    def embed(self, texts):
        return self._vectors


@pytest.fixture
def width() -> int:
    return settings.EMBEDDING_DIMENSION


def _use(monkeypatch, vectors):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setattr(embeddings, "resolve_provider", lambda: FakeProvider(vectors))


def test_vectors_are_normalised_to_unit_length(monkeypatch, width):
    _use(monkeypatch, [[3.0] + [0.0] * (width - 1)])

    vector = embeddings.get_bge_embedding("anything")

    assert math.isclose(sum(v * v for v in vector) ** 0.5, 1.0, rel_tol=1e-9), (
        "cosine distance only agrees with dot product on unit vectors, and the tuned "
        "thresholds assume it"
    )


def test_a_wrong_width_is_rejected_rather_than_stored(monkeypatch, width):
    _use(monkeypatch, [[1.0] * (width - 1)])

    with pytest.raises(RuntimeError, match="Embedding generation failed"):
        embeddings.get_bge_embedding("anything")


def test_a_zero_vector_is_rejected(monkeypatch, width):
    _use(monkeypatch, [[0.0] * width])

    with pytest.raises(RuntimeError, match="Embedding generation failed"):
        embeddings.get_bge_embedding("anything")


def test_a_short_batch_is_rejected(monkeypatch, width):
    """Silently dropping a vector would misalign chunks and their embeddings."""
    _use(monkeypatch, [[1.0] * width])

    with pytest.raises(RuntimeError, match="Embedding batch generation failed"):
        embeddings.get_bge_embeddings(["one", "two"])


def test_an_unknown_runtime_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "tensorflow")

    with pytest.raises(base.EmbeddingUnavailable, match="not one of"):
        embeddings.resolve_provider()


def test_runtime_does_not_change_which_model_is_selected(monkeypatch):
    """Runtime and model identity are orthogonal — that is the point of the split."""
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "BAAI/bge-m3")

    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "torch")
    assert embeddings.resolve_provider().name == "torch"

    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "onnx")
    assert embeddings.resolve_provider().name == "onnx"


def test_a_hosted_model_ignores_the_local_runtime(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "onnx")

    assert embeddings.resolve_provider().name == "hosted"


def test_a_baked_directory_is_loaded_instead_of_the_repo_id(monkeypatch, tmp_path):
    """The bake downloads one copy of the weights; a directory load cannot fetch the
    duplicate pytorch_model.bin the repo-id path pulls in."""
    monkeypatch.setattr(local_torch.settings, "EMBEDDING_TORCH_DIR", str(tmp_path))

    assert local_torch._model_source() == str(tmp_path)


def test_a_missing_baked_directory_falls_back_to_the_repo_id(monkeypatch):
    """A bake-less image (BAKE_EMBEDDING_MODEL=false) must behave like a local checkout
    rather than fail on a directory that was never populated."""
    monkeypatch.setattr(local_torch.settings, "EMBEDDING_TORCH_DIR", "/no/such/dir")
    monkeypatch.setattr(local_torch.settings, "EMBEDDING_MODEL", "BAAI/bge-m3")

    assert local_torch._model_source() == "BAAI/bge-m3"


def test_mock_short_circuits_without_touching_a_backend(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "mock")

    def explode():
        raise AssertionError("mock must not reach a backend")

    monkeypatch.setattr(embeddings, "resolve_provider", explode)

    assert embeddings.get_bge_embedding("x") == [0.0] * settings.EMBEDDING_DIMENSION


# ── Parity ───────────────────────────────────────────────────────────────────
# The two runtimes truncate at different points: sentence-transformers reads the model's
# own max_seq_length (8192 for bge-m3), while the ONNX tokenizer truncates at
# EMBEDDING_MAX_TOKENS (512). Every short input below that threshold passes parity even
# when the runtimes disagree about truncation — and a real chunk is ~1500 chars of
# Vietnamese, which is over it. Parity that holds only for short strings proves nothing
# about the corpus, so the gate needs one input the truncation actually bites. Sized to
# stay over EMBEDDING_MAX_TOKENS for plausible values of the setting, not just today's.
_LONG_TEXT = (
    "Quy trinh xin nghi phep cua nhan vien yeu cau nguoi lam don gui cho truong phong "
    "truoc it nhat ba ngay lam viec va truong phong phai xac nhan don bang van ban "
    "truoc khi de nghi nghi phep duoc phe duyet boi phong nhan su. "
) * 40


def _both_backends_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import sentence_transformers  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return os.path.isdir(settings.EMBEDDING_ONNX_DIR)


@pytest.mark.skipif(
    not _both_backends_available(),
    reason="needs the 'ml' and 'onnx' groups plus a baked export; runs in the image, not on a laptop",
)
@pytest.mark.parametrize(
    "text",
    [
        "annual leave policy",
        "Quy trinh xin nghi phep cua nhan vien",  # Vietnamese is the case that decides it
        _LONG_TEXT,  # over EMBEDDING_MAX_TOKENS — the truncation divergence case
        "",
    ],
)
def test_onnx_matches_torch_closely_enough_to_skip_re_embedding(monkeypatch, text):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "torch")
    reference = embeddings.get_bge_embedding(text)

    monkeypatch.setattr(embeddings.settings, "EMBEDDING_RUNTIME", "onnx")
    candidate = embeddings.get_bge_embedding(text)

    similarity = sum(a * b for a, b in zip(reference, candidate))
    assert similarity >= 0.999, (
        f"cosine similarity {similarity:.5f} — below this the two runtimes are different "
        "vector spaces, so switching EMBEDDING_RUNTIME requires re-embedding every chunk"
    )
