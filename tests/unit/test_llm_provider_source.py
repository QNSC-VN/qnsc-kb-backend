from src.core.config import settings
from src.domain.llm_config import RuntimeLLMConfig, get_runtime_config, public_config, set_runtime_config


def teardown_function() -> None:
    set_runtime_config(None)


def test_llm_provider_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "environment-key")
    monkeypatch.setattr(settings, "LLM_MODEL", "llama-3.3-70b-versatile")

    set_runtime_config(None)

    assert get_runtime_config() is None
    assert public_config(None)["source"] == "none"
    assert public_config(None)["enabled"] is False


def test_llm_provider_uses_saved_runtime_configuration():
    set_runtime_config(RuntimeLLMConfig(
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1/chat/completions",
        api_key="saved-admin-key",
    ))

    config = public_config(None)

    assert config["source"] == "admin"
    assert config["enabled"] is True
    assert config["provider"] == "openai"
    assert config["model"] == "gpt-4o-mini"
