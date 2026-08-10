from typing import AsyncGenerator
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.core.config import settings
from src.models import User
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
    # Runtime schema creation and ALTER statements are intentionally forbidden.
    # The deployment entrypoint runs Alembic before the application starts.
    if settings.AUTO_CREATE_SCHEMA:
        raise RuntimeError("AUTO_CREATE_SCHEMA is disabled; run Alembic migrations before starting the application")
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
