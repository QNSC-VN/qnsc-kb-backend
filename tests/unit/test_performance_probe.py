import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from performance_probe import (
    benchmark_question,
    percentile,
    provider_gate_failed,
    provider_is_configured,
)


def test_percentile_is_deterministic_and_empty_safe():
    assert percentile([], 0.95) == 0.0
    assert percentile([30.0, 10.0, 20.0], 0.50) == 20.0
    assert percentile([30.0, 10.0, 20.0], 0.95) == 30.0


def test_provider_flag_requires_real_configuration():
    assert not provider_is_configured({"provider": "openai", "configured": False})
    assert not provider_is_configured(
        {"configured": True, "enabled": False, "api_key_configured": True}
    )
    assert provider_is_configured(
        {"configured": True, "enabled": True, "api_key_configured": True}
    )


def test_provider_gate_can_be_required_for_release_evidence():
    assert provider_gate_failed({"provider_backed": False}, True)
    assert provider_gate_failed({"provider_backed": None}, True)
    assert not provider_gate_failed({"provider_backed": False}, False)
    assert not provider_gate_failed({"provider_backed": True}, True)


def test_benchmark_question_changes_cache_text_without_adding_retrieval_terms():
    base = "What is the retention period?"
    variant = benchmark_question(base, 2, True)

    assert variant != base
    assert variant.split() == base.split()
    assert benchmark_question(base, 2, False) == base
