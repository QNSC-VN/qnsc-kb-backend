from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any

class Settings(BaseSettings):
    PROJECT_NAME: str = "QNSC Knowledge Base"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "super-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/qnsc_kb"
    REDIS_URL: str = "redis://localhost:6379/0"

    OPENAI_API_KEY: str | None = None
    GROQ_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemma-4-26b-a4b-it"
    GEMINI_API_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
    GEMINI_THINKING_LEVEL: str = "minimal"
    GEMINI_MAX_OUTPUT_TOKENS: int = 8192
    LLM_TIMEOUT_SECONDS: float = 90.0
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIMENSION: int | None = None
    LLM_MODEL: str = "gemma-4-26b-a4b-it"
    RESTRUCTURE_ENABLED: bool = True
    RESTRUCTURE_MODEL: str | None = None
    RESTRUCTURE_MAX_CHARS: int = 60000
    RESTRUCTURE_TIMEOUT_SECONDS: float = 120.0
    AI_RATE_LIMIT_PER_MINUTE: int = 30
    VECTOR_DISTANCE_THRESHOLD: float = 0.45
    PROMPT_VERSION: str = "v1.0"
    RETRIEVAL_VERSION: str = "v1.1-hybrid-rerank"
    RERANKER_VERSION: str = "v1.0-lexical"
    OIDC_ISSUER_URL: str | None = None
    OIDC_CLIENT_ID: str | None = None
    OIDC_CLIENT_SECRET: str | None = None
    OIDC_REDIRECT_URI: str | None = None
    OIDC_SCOPES: str = "openid profile email"
    ALLOWED_EMAIL_DOMAINS: str = ""
    ALLOW_SELF_REGISTRATION: bool = True
    PADDLEOCR_LANG: str = "en"
    MARKITDOWN_ENABLED: bool = True
    SOURCE_STORAGE_PATH: str = "/app/storage/sources"
    CONNECTOR_ROOT_PATH: str = "/app/storage/connectors"

    def model_post_init(self, __context: Any) -> None:
        if self.EMBEDDING_DIMENSION is None:
            model = self.EMBEDDING_MODEL.lower()
            if "bge-m3" in model:
                self.EMBEDDING_DIMENSION = 1024
            elif "minilm" in model:
                self.EMBEDDING_DIMENSION = 384
            elif "text-embedding-3-small" in model or "ada-002" in model:
                self.EMBEDDING_DIMENSION = 1536
            else:
                self.EMBEDDING_DIMENSION = 1024

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
