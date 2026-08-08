from typing import AsyncGenerator
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.core.config import settings
from src.models import Base, User
from src.repositories.user import UserRepository
from src.domain.rbac import AuthorizationService, bootstrap_rbac
from src.domain.llm_config import load_runtime_config

engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={
        "timeout": 5,
        "command_timeout": 5,
    },
)
SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine, class_=AsyncSession)

# OAuth2 scheme point to login route
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False)

async def init_db() -> None:
    if settings.AUTO_CREATE_SCHEMA:
        async with engine.begin() as conn:
            # Enable pgvector extension
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            # Create all tables defined in models
            await conn.run_sync(Base.metadata.create_all)
            # The project previously bootstrapped its schema with create_all.
            # Keep existing development databases forward-compatible; in
            # production Alembic is the only schema authority.
            for statement in ((
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
            "ALTER TABLE ai_cache ADD COLUMN IF NOT EXISTS article_ids JSON",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS needs_update BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS index_status VARCHAR(30) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS index_error TEXT",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS external_id VARCHAR(120)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_articles_external_id ON articles (external_id)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255)",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255)",
            "UPDATE users SET company_domain = split_part(lower(email), '@', 2) WHERE company_domain IS NULL OR company_domain = ''",
            "UPDATE articles a SET company_domain = COALESCE((SELECT u.company_domain FROM users u WHERE u.id = a.owner_id), 'local') WHERE a.company_domain IS NULL OR a.company_domain = ''",
            "ALTER TABLE users ALTER COLUMN company_domain SET DEFAULT 'local'",
            "ALTER TABLE users ALTER COLUMN company_domain SET NOT NULL",
            "ALTER TABLE articles ALTER COLUMN company_domain SET DEFAULT 'local'",
            "ALTER TABLE articles ALTER COLUMN company_domain SET NOT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255) NOT NULL DEFAULT 'local'",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_subject VARCHAR(255)",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_access_token TEXT",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_expires_at TIMESTAMP",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_state_hash VARCHAR(64)",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS oauth_state_expires_at TIMESTAMP",
            "ALTER TABLE connectors ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(30) NOT NULL DEFAULT 'active'",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS related_article_ids JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS similarity_level VARCHAR(30)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS similarity_matches JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS requires_update_confirmation BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS update_target_article_id UUID REFERENCES articles(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS related_article_ids JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS tags JSON",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS external_document_id UUID REFERENCES external_documents(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255) NOT NULL DEFAULT 'local'",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS dept VARCHAR(100)",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS assigned_approver_id UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS assigned_by UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP",
            "ALTER TABLE pending_drafts ADD COLUMN IF NOT EXISTS review_note TEXT",
            "UPDATE pending_drafts p SET company_domain = COALESCE((SELECT u.company_domain FROM users u WHERE u.id = p.created_by), 'local') WHERE p.company_domain IS NULL OR p.company_domain = ''",
            "CREATE INDEX IF NOT EXISTS ix_pending_drafts_company_domain ON pending_drafts (company_domain)",
            "CREATE INDEX IF NOT EXISTS ix_pending_drafts_assigned_approver_id ON pending_drafts (assigned_approver_id)",
            "ALTER TABLE access_groups ADD COLUMN IF NOT EXISTS company_domain VARCHAR(255) NOT NULL DEFAULT 'local'",
            "CREATE INDEX IF NOT EXISTS ix_access_groups_company_domain ON access_groups (company_domain)",
            )):
                await conn.execute(text(statement))
    async with SessionLocal() as db:
        # Startup bootstrap is an internal maintenance operation.  It must
        # remain able to seed roles after production RLS has been migrated.
        await set_database_context(db, None, True)
        await bootstrap_rbac(db)
        await load_runtime_config(db)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            # A pooled connection must never retain another request's tenant
            # settings. Roll back any open work, reset session variables, and
            # return the connection cleanly to the pool.
            try:
                await session.rollback()
                await session.execute(text("RESET app.company_domain"))
                await session.execute(text("RESET app.global_admin"))
                await session.execute(text("RESET app.global_article_access"))
                await session.execute(text("RESET app.global_identity_access"))
                await session.execute(text("RESET app.global_connector_access"))
                await session.execute(text("RESET app.global_governance_access"))
                await session.execute(text("RESET app.user_id"))
                await session.commit()
            except Exception:
                await session.rollback()


async def set_database_context(
    db: AsyncSession,
    company_domain: str | None,
    global_admin: bool = False,
    user_id: str | None = None,
    global_article_access: bool = False,
    global_identity_access: bool = False,
    global_connector_access: bool = False,
    global_governance_access: bool = False,
) -> None:
    await db.execute(
        text("SELECT set_config('app.company_domain', :company_domain, false), set_config('app.global_admin', :global_admin, false), set_config('app.global_article_access', :global_article_access, false), set_config('app.global_identity_access', :global_identity_access, false), set_config('app.global_connector_access', :global_connector_access, false), set_config('app.global_governance_access', :global_governance_access, false), set_config('app.user_id', :user_id, false)"),
        {
            "company_domain": company_domain or "",
            "global_admin": "true" if global_admin else "false",
            "global_article_access": "true" if global_article_access else "false",
            "global_identity_access": "true" if global_identity_access else "false",
            "global_connector_access": "true" if global_connector_access else "false",
            "global_governance_access": "true" if global_governance_access else "false",
            "user_id": user_id or "",
        },
    )

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
    access_token_cookie: str | None = Cookie(default=None, alias="access_token"),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = token or access_token_cookie
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") == "refresh":
            raise credentials_exception
        email = payload.get("sub")
        if not isinstance(email, str):
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    email = email.strip().lower()
    if "@" not in email:
        raise credentials_exception
    # A signed token has already authenticated this domain.  Set it before
    # loading the user so identity and membership RLS policies do not require
    # a temporary global bypass.
    await set_database_context(db, email.rsplit("@", 1)[1])
    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(email)
    if user is None:
        raise credentials_exception
    if not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated")
    if int(payload.get("av", 0)) != user.auth_version:
        raise credentials_exception
    if not user.roles:
        await bootstrap_rbac(db)
        user = await user_repo.get_by_email(email)
    await set_database_context(
        db,
        user.company_domain,
        AuthorizationService.is_global_administrator(user),
        str(user.id),
        AuthorizationService.has_global_article_access(user),
        AuthorizationService.has_global_identity_management(user),
        AuthorizationService.has_global_connector_management(user),
        AuthorizationService.has_global_governance_access(user),
    )
    return user

def require_permission(permission: str, scope: str = "company"):
    def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not AuthorizationService.has_permission(current_user, permission, requested_scope=scope):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing permission: {permission}")
        return current_user
    return permission_checker


def require_role(roles: list[str]):
    # Compatibility dependency for existing routers. The decision is now made
    # from the seeded permission catalog, not only from users.role.
    permission = "governance.read"
    if roles == ["Admin"]:
        permission = "role.manage"
    elif set(roles) <= {"Admin", "CEO"}:
        permission = "user.manage"
    elif set(roles) <= {"Admin", "Reviewer"}:
        permission = "article.review"

    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if not AuthorizationService.has_permission(current_user, permission, requested_scope="company"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required permission: {permission}"
            )
        return current_user
    return role_checker
