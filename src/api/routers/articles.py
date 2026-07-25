import uuid
from datetime import datetime
from typing import Any, Sequence
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user
from src.models import User
from src.repositories.article import ArticleRepository
from src.repositories.user import UserRepository
from src.domain.articles import ArticleService

router = APIRouter()

# Schema definitions
class ArticleCreate(BaseModel):
    title: str
    body_md: str
    dept: str
    domain: str
    type: str  # POLICY, SOP, DECISION, FAQ, RCA, HOWTO, PLAYBOOK, REFERENCE
    sensitivity: str  # public, internal, confidential, restricted
    tags: list[str] = []
    access_group_ids: list[uuid.UUID] | None = None
    next_review: datetime | None = None

class ArticleUpdate(BaseModel):
    title: str | None = None
    body_md: str | None = None
    dept: str | None = None
    domain: str | None = None
    type: str | None = None
    sensitivity: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    access_group_ids: list[uuid.UUID] | None = None
    next_review: datetime | None = None

class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    bitmask_position: int
    class Config:
        from_attributes = True

class TagResponse(BaseModel):
    tag: str
    class Config:
        from_attributes = True

class ArticleResponse(BaseModel):
    id: uuid.UUID
    title: str
    body_md: str
    dept: str
    domain: str
    type: str
    sensitivity: str
    status: str
    version: int
    created_at: datetime
    next_review: datetime | None = None
    last_reviewed: datetime | None = None
    access_groups: list[GroupResponse] = []
    tags: list[TagResponse] = []

    class Config:
        from_attributes = True

class VersionResponse(BaseModel):
    id: uuid.UUID
    article_id: uuid.UUID
    version: int
    snapshot: dict
    created_at: datetime
    class Config:
        from_attributes = True

@router.post("/", response_model=ArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_article(
    article_in: ArticleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.create_article(
        user=current_user,
        title=article_in.title,
        body_md=article_in.body_md,
        dept=article_in.dept,
        domain=article_in.domain,
        type_=article_in.type,
        sensitivity=article_in.sensitivity,
        tags=article_in.tags,
        access_group_ids=article_in.access_group_ids,
        next_review=article_in.next_review
    )

@router.get("/", response_model=list[ArticleResponse])
async def list_articles(
    dept: str | None = Query(None),
    type_: str | None = Query(None, alias="type"),
    sensitivity: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    return await article_repo.list_articles(
        user=current_user,
        dept=dept,
        type_=type_,
        sensitivity=sensitivity,
        status=status,
        search_query=q
    )

@router.get("/{id}", response_model=ArticleResponse)
async def get_article(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.get_article(current_user, id)

@router.put("/{id}", response_model=ArticleResponse)
async def update_article(
    id: uuid.UUID,
    article_in: ArticleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.update_article(
        user=current_user,
        article_id=id,
        title=article_in.title,
        body_md=article_in.body_md,
        dept=article_in.dept,
        domain=article_in.domain,
        type_=article_in.type,
        sensitivity=article_in.sensitivity,
        status_=article_in.status,
        tags=article_in.tags,
        access_group_ids=article_in.access_group_ids,
        next_review=article_in.next_review
    )

@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    await article_service.soft_delete_article(current_user, id)

@router.get("/{id}/versions", response_model=list[VersionResponse])
async def list_versions(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.get_history(current_user, id)

@router.get("/{id}/versions/{version_num}", response_model=VersionResponse)
async def get_version(
    id: uuid.UUID,
    version_num: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.get_version(current_user, id, version_num)
