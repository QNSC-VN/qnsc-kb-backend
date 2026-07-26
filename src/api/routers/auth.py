import uuid
from typing import Any
from fastapi import APIRouter, Depends, status, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user, require_role
from src.repositories.user import UserRepository
from src.domain.auth import AuthService
from src.core.config import settings
from src.repositories.audit import AuditRepository

router = APIRouter()

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    dept: str | None
    role: str
    company_domain: str
    active: bool
    
    class Config:
        from_attributes = True

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: UserResponse

class ManagedUserCreate(BaseModel):
    email: EmailStr
    name: str
    password: str
    dept: str | None = None
    role: str = "Staff"


class ManagedUserUpdate(BaseModel):
    name: str | None = None
    dept: str | None = None
    role: str | None = None
    password: str | None = None
    email: EmailStr | None = None
    active: bool | None = None


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    current_user: Any = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    users = await UserRepository(db).list_users()
    return [user for user in users if current_user.role == "Admin" or user.company_domain == current_user.company_domain]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_managed_user(
    user_in: ManagedUserCreate,
    current_user: Any = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    domain = str(user_in.email).lower().rsplit("@", 1)[-1]
    if current_user.role == "CEO" and domain != current_user.company_domain:
        raise HTTPException(status_code=403, detail="CEOs can only add employees to their own company")
    if current_user.role == "CEO" and user_in.role not in {"Staff", "Reviewer", "Department Owner"}:
        raise HTTPException(status_code=403, detail="CEOs cannot create Admin or CEO accounts")
    password = user_in.password
    user = await AuthService(UserRepository(db)).register_user(
        email=str(user_in.email), name=user_in.name, password=password,
        dept=user_in.dept, role=user_in.role,
        allow_privileged_role=True,
    )
    await AuditRepository(db).record(current_user.id, "user_create", "user", str(user.id))
    return user


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_managed_user(
    user_id: uuid.UUID,
    user_in: ManagedUserUpdate,
    current_user: Any = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user.role == "CEO" and user.company_domain != current_user.company_domain:
        raise HTTPException(status_code=403, detail="CEOs can only manage their own company")
    if user.id == current_user.id and user_in.role and user_in.role != "Admin":
        raise HTTPException(status_code=400, detail="You cannot remove your own Admin role")
    if user_in.name is not None:
        user.name = user_in.name
    if user_in.dept is not None:
        user.dept = user_in.dept
    if user_in.role is not None:
        if user_in.role not in {"Admin", "CEO", "Department Owner", "Reviewer", "Staff"}:
            raise HTTPException(status_code=422, detail="Invalid role")
        if current_user.role == "CEO" and user_in.role in {"Admin", "CEO"}:
            raise HTTPException(status_code=403, detail="CEOs cannot assign Admin or CEO roles")
        user.role = user_in.role
    if user_in.email is not None:
        new_email = str(user_in.email).lower()
        if current_user.role == "CEO" and new_email.rsplit("@", 1)[-1] != current_user.company_domain:
            raise HTTPException(status_code=403, detail="Employee email must remain in the company domain")
        user.email = new_email
        user.company_domain = new_email.rsplit("@", 1)[-1]
    if user_in.active is not None:
        if user.id == current_user.id and not user_in.active:
            raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
        user.active = user_in.active
    if user_in.password:
        from src.core.security import get_password_hash
        user.password_hash = get_password_hash(user_in.password)
    updated = await repo.update(user)
    await AuditRepository(db).record(current_user.id, "user_update", "user", str(user.id))
    return updated


@router.delete("/users/{user_id}", response_model=UserResponse)
async def deactivate_user(
    user_id: uuid.UUID,
    current_user: Any = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user.role == "CEO" and user.company_domain != current_user.company_domain:
        raise HTTPException(status_code=403, detail="CEOs can only manage their own company")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    user.active = False
    if user.role == "CEO":
        user.role = "Staff"
    updated = await repo.update(user)
    await AuditRepository(db).record(current_user.id, "user_deactivate", "user", str(user.id))
    return updated


@router.post("/companies/{company_domain}/ceo", response_model=UserResponse)
async def assign_company_ceo(
    company_domain: str,
    user_id: uuid.UUID,
    current_user: Any = Depends(require_role(["Admin"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    repo = UserRepository(db)
    users = [user for user in await repo.list_users() if user.company_domain == company_domain.lower()]
    target = next((user for user in users if user.id == user_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Employee not found in this company")
    for user in users:
        if user.role == "CEO" and user.id != target.id:
            user.role = "Staff"
            await repo.update(user)
            await AuditRepository(db).record(current_user.id, "ceo_change", "user", str(user.id))
    target.role = "CEO"
    target.active = True
    target = await repo.update(target)
    await AuditRepository(db).record(current_user.id, "ceo_change", "user", str(target.id))
    return target

@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
) -> Any:
    # OAuth2 request form uses username as email field
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    user = await auth_service.authenticate_user(email=form_data.username, password=form_data.password)
    token = auth_service.create_token(user)
    return {
        "access_token": token,
        "refresh_token": auth_service.create_refresh_token(user),
        "token_type": "bearer",
        "user": user
    }

class RefreshRequest(BaseModel):
    refresh_token: str

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(req: RefreshRequest, db: AsyncSession = Depends(get_db)) -> Any:
    try:
        payload = jwt.decode(req.refresh_token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "refresh" or not payload.get("sub"):
            raise JWTError("Not a refresh token")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired") from exc
    user = await UserRepository(db).get_by_email(str(payload["sub"]))
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    service = AuthService(UserRepository(db))
    return {"access_token": service.create_token(user), "refresh_token": service.create_refresh_token(user), "token_type": "bearer", "user": user}

@router.get("/me", response_model=UserResponse)
async def read_current_user(current_user: Any = Depends(get_current_user)) -> Any:
    return current_user


@router.get("/oidc/config")
async def oidc_config() -> dict[str, object]:
    """Expose non-secret OIDC configuration for a future Entra/Workspace login UI."""
    return {
        "enabled": bool(settings.OIDC_ISSUER_URL and settings.OIDC_CLIENT_ID and settings.OIDC_REDIRECT_URI),
        "issuer_url": settings.OIDC_ISSUER_URL,
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "scopes": settings.OIDC_SCOPES.split(),
    }
