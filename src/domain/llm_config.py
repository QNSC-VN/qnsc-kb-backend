"""Persisted and runtime configuration for the workspace LLM provider."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.secrets import decrypt_secret, encrypt_secret
from src.models.ops import LLMProviderConfig

SUPPORTED_PROVIDERS = {"openai", "glm", "groq", "gemini"}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    # NOT a chat-completions endpoint like the others: the Gemini transport in
    # llm_client.py appends "/models/<model>:generateContent" itself, so this is the API
    # ROOT. Pointing it at a full path produces a 404 on every call.
    "gemini": settings.GEMINI_API_BASE_URL.rstrip("/"),
}


@dataclass(frozen=True)
class RuntimeLLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str


_runtime_config: RuntimeLLMConfig | None = None
_runtime_config_loaded = False


def encrypt_api_key(api_key: str) -> str:
    encrypted = encrypt_secret(api_key)
    if encrypted is None:
        raise ValueError("An API key is required")
    return encrypted


def decrypt_api_key(value: str | None) -> str | None:
    return decrypt_secret(value)


def _environment_config() -> RuntimeLLMConfig | None:
    provider = (settings.LLM_PROVIDER or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        if settings.OPENAI_API_KEY and settings.OPENAI_API_KEY != "mock":
            provider = "openai"
        elif settings.GROQ_API_KEY and settings.GROQ_API_KEY != "mock":
            provider = "groq"
        elif settings.GEMINI_API_KEY and settings.GEMINI_API_KEY != "mock":
            # Last, so an explicitly configured provider always wins — but present, so a
            # deployment holding only a Gemini key gets real answers. Without this branch
            # GEMINI_API_KEY powered embeddings ONLY: resolve_provider() returned None and
            # every question fell back to a canned extract of the top chunk, which reads
            # like a working answer and is why nobody noticed.
            provider = "gemini"
        else:
            return None
    api_key = (
        settings.OPENAI_API_KEY if provider == "openai"
        else settings.GROQ_API_KEY if provider == "groq"
        else settings.GEMINI_API_KEY if provider == "gemini"
        else settings.GLM_API_KEY
    )
    if not api_key or api_key == "mock":
        return None
    model = settings.LLM_MODEL
    if provider == "groq" and model == "gpt-4o":
        model = "llama-3.3-70b-versatile"
    if provider == "gemini":
        # LLM_MODEL names an OpenAI-shaped model by default; Gemini has its own setting.
        model = settings.GEMINI_MODEL
    return RuntimeLLMConfig(provider, model, (settings.LLM_BASE_URL or DEFAULT_BASE_URLS[provider]).rstrip("/"), api_key)


def set_runtime_config(config: RuntimeLLMConfig | None) -> None:
    global _runtime_config, _runtime_config_loaded
    _runtime_config = config
    _runtime_config_loaded = True


def get_runtime_config() -> RuntimeLLMConfig | None:
    return _runtime_config if _runtime_config_loaded else _environment_config()


async def load_runtime_config(db: AsyncSession) -> None:
    result = await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.config_key == "workspace"))
    row = result.scalar_one_or_none()
    if row is None:
        set_runtime_config(_environment_config())
        return
    key = decrypt_api_key(row.encrypted_api_key)
    set_runtime_config(RuntimeLLMConfig(row.provider, row.model, row.base_url.rstrip("/"), key) if row.enabled and key else None)


def public_config(row: LLMProviderConfig | None) -> dict:
    runtime = get_runtime_config()
    if row is None:
        return {
            "configured": runtime is not None,
            "source": "environment" if runtime else "none",
            "enabled": runtime is not None,
            "provider": runtime.provider if runtime else "openai",
            "model": runtime.model if runtime else settings.LLM_MODEL,
            "base_url": runtime.base_url if runtime else DEFAULT_BASE_URLS["openai"],
            "allow_custom_base_url": settings.LLM_ALLOW_CUSTOM_BASE_URL,
            "api_key_configured": runtime is not None,
            "api_key_hint": "Configured" if runtime else None,
        }
    key = decrypt_api_key(row.encrypted_api_key)
    return {
        "configured": bool(key),
        "source": "admin",
        "enabled": row.enabled,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "allow_custom_base_url": settings.LLM_ALLOW_CUSTOM_BASE_URL,
        "api_key_configured": bool(key),
        "api_key_hint": "Configured" if key else None,
    }
