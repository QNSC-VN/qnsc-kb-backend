from typing import Any
import uuid
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user
from src.models import User
from src.domain.meta import MetaService

router = APIRouter()

class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    bitmask_position: int

    class Config:
        from_attributes = True

@router.get("/tags")
async def get_tags(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    service = MetaService(db)
    return await service.get_all_tags()

@router.get("/glossary")
async def get_glossary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    service = MetaService(db)
    return await service.get_glossary()

@router.get("/taxonomy")
async def get_taxonomy(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    service = MetaService(db)
    return await service.get_taxonomy()

@router.get("/groups", response_model=list[GroupResponse])
async def get_groups(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    from src.repositories.user import UserRepository
    user_repo = UserRepository(db)
    return await user_repo.get_all_groups()

