from typing import AsyncGenerator
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.core.config import settings
from src.models import Base, User
from src.repositories.user import UserRepository

engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={
        "timeout": 5,
        "command_timeout": 5,
    },
)
SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine, class_=AsyncSession)

# OAuth2 scheme point to login route
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

async def init_db() -> None:
    async with engine.begin() as conn:
        # Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        # Create all tables defined in models
        await conn.run_sync(Base.metadata.create_all)
        # The project previously bootstrapped its schema with create_all. Keep
        # existing development databases forward-compatible until Alembic is
        # introduced for production migrations.
        for statement in (
            "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS storage_key VARCHAR(512)",
            "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS original_filename VARCHAR(255)",
            "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS mime_type VARCHAR(150)",
            "ALTER TABLE document_sources ADD COLUMN IF NOT EXISTS page_texts JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS storage_key VARCHAR(512)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS original_filename VARCHAR(255)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS mime_type VARCHAR(150)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS page_texts JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS restructured_body_md TEXT",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS restructure_status VARCHAR(40) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS restructure_model VARCHAR(100)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS restructure_error TEXT",
            "ALTER TABLE parent_chunks ADD COLUMN IF NOT EXISTS page_number INTEGER",
            "ALTER TABLE article_chunks ADD COLUMN IF NOT EXISTS page_number INTEGER",
            "ALTER TABLE ai_usage_logs ADD COLUMN IF NOT EXISTS retrieved_chunk_ids TEXT",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS needs_update BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS index_status VARCHAR(30) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS index_error TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255)",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255)",
            "UPDATE users SET company_domain = split_part(lower(email), '@', 2) WHERE company_domain IS NULL OR company_domain = ''",
            "UPDATE articles a SET company_domain = COALESCE((SELECT u.company_domain FROM users u WHERE u.id = a.owner_id), 'local') WHERE a.company_domain IS NULL OR a.company_domain = ''",
            "ALTER TABLE users ALTER COLUMN company_domain SET DEFAULT 'local'",
            "ALTER TABLE users ALTER COLUMN company_domain SET NOT NULL",
            "ALTER TABLE articles ALTER COLUMN company_domain SET DEFAULT 'local'",
            "ALTER TABLE articles ALTER COLUMN company_domain SET NOT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255) NOT NULL DEFAULT 'local'",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(30) NOT NULL DEFAULT 'active'",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS related_article_ids JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS similarity_level VARCHAR(30)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS similarity_matches JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS requires_update_confirmation BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS update_target_article_id UUID REFERENCES articles(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS related_article_ids JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS tags JSON",
        ):
            await conn.execute(text(statement))

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") == "refresh":
            raise credentials_exception
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(email)
    if user is None:
        raise credentials_exception
    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated")
    return user

def require_role(roles: list[str]):
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {roles}. Current role: {current_user.role}"
            )
        return current_user
    return role_checker
