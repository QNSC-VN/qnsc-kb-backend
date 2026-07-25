import uuid
from typing import Any
from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user
from src.repositories.user import UserRepository
from src.domain.auth import AuthService

router = APIRouter()

class UserRegister(BaseModel):
    email: EmailStr
    name: str
    password: str
    dept: str | None = None
    role: str = "Staff"  # Staff, Reviewer, Department Owner, Admin

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    dept: str | None
    role: str
    
    class Config:
        from_attributes = True

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserRegister, db: AsyncSession = Depends(get_db)) -> Any:
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    user = await auth_service.register_user(
        email=user_in.email,
        name=user_in.name,
        password=user_in.password,
        dept=user_in.dept,
        role=user_in.role
    )
    return user

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
        "token_type": "bearer",
        "user": user
    }

@router.get("/me", response_model=UserResponse)
async def read_current_user(current_user: Any = Depends(get_current_user)) -> Any:
    return current_user
