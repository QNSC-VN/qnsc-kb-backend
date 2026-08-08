import ipaddress
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db, require_permission
from src.core.config import settings
from src.domain.llm_config import DEFAULT_BASE_URLS, SUPPORTED_PROVIDERS, RuntimeLLMConfig, encrypt_api_key, get_runtime_config, public_config, set_runtime_config, decrypt_api_key
from src.models import User
from src.models.ops import LLMProviderConfig

router = APIRouter()


class LLMConfigUpdate(BaseModel):
    enabled: bool = True
    provider: Literal["openai", "glm", "groq"]
    model: str = Field(min_length=1, max_length=150)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)


def _clean_base_url(provider: str, base_url: str | None) -> str:
    default_url = DEFAULT_BASE_URLS[provider]
    value = (base_url or default_url).strip().rstrip("/")
    if value != default_url and not settings.LLM_ALLOW_CUSTOM_BASE_URL:
        raise HTTPException(
            status_code=422,
            detail="Custom LLM endpoints are disabled. Set LLM_ALLOW_CUSTOM_BASE_URL=true only for a trusted, public endpoint.",
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The LLM endpoint has an invalid port") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(status_code=422, detail="The LLM endpoint must be a credential-free HTTPS URL")
    if port not in (None, 443):
        raise HTTPException(status_code=422, detail="The LLM endpoint must use port 443")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise HTTPException(status_code=422, detail="The LLM endpoint cannot target a local network host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise HTTPException(status_code=422, detail="The LLM endpoint cannot target a private or reserved address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


@router.get("/config")
async def get_llm_config(current_user: User = Depends(require_permission("role.manage", scope="global")), db: AsyncSession = Depends(get_db)) -> Any:
    result = await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.config_key == "workspace"))
    return public_config(result.scalar_one_or_none())


@router.put("/config")
async def update_llm_config(payload: LLMConfigUpdate, current_user: User = Depends(require_permission("role.manage", scope="global")), db: AsyncSession = Depends(get_db)) -> Any:
    if payload.provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported LLM provider")
    result = await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.config_key == "workspace"))
    row = result.scalar_one_or_none()
    existing_key = decrypt_api_key(row.encrypted_api_key) if row else None
    api_key = (payload.api_key or "").strip() or existing_key
    if payload.enabled and not api_key:
        raise HTTPException(status_code=422, detail="An API key is required when the LLM provider is enabled")
    base_url = _clean_base_url(payload.provider, payload.base_url)
    if row is None:
        row = LLMProviderConfig(config_key="workspace", model=payload.model.strip(), base_url=base_url)
        db.add(row)
    row.enabled = payload.enabled
    row.provider = payload.provider
    row.model = payload.model.strip()
    row.base_url = base_url
    if payload.api_key and payload.api_key.strip():
        row.encrypted_api_key = encrypt_api_key(api_key)
    elif row.encrypted_api_key is None and api_key:
        row.encrypted_api_key = encrypt_api_key(api_key)
    await db.commit()
    await db.refresh(row)
    set_runtime_config(RuntimeLLMConfig(row.provider, row.model, row.base_url, api_key) if row.enabled and api_key else None)
    return public_config(row)
