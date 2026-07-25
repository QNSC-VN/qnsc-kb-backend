from typing import Any
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user
from src.models import User
from src.repositories.chunk import ChunkRepository
from src.repositories.governance import GovernanceRepository
from src.domain.search_service import SearchService

router = APIRouter()

@router.get("")
async def search(
    q: str = Query("", description="Search query string"),
    dept: str | None = Query(None),
    sensitivity: str | None = Query(None),
    limit: int = Query(5, ge=1, le=20),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    chunk_repo = ChunkRepository(db)
    gov_repo = GovernanceRepository(db)
    search_service = SearchService(chunk_repo, gov_repo)
    
    filters = {}
    if dept:
        filters["dept"] = dept
    if sensitivity:
        filters["sensitivity"] = sensitivity

    return await search_service.search(
        user=current_user,
        query=q,
        filters=filters,
        limit=limit
    )
