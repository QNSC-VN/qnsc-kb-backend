from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any

class Settings(BaseSettings):
    PROJECT_NAME: str = "QNSC Knowledge Base"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "super-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/qnsc_kb"
    REDIS_URL: str = "redis://localhost:6379/0"

    OPENAI_API_KEY: str | None = None
    GROQ_API_KEY: str | None = None
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIMENSION: int | None = None
    LLM_MODEL: str = "gpt-4o"

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
