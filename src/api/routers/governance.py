import uuid
from typing import Any
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user, require_role
from src.models import User
from src.repositories.governance import GovernanceRepository
from src.repositories.article import ArticleRepository
from src.repositories.ops import OpsRepository
from src.domain.governance import GovernanceService

router = APIRouter()

class ApproveRequest(BaseModel):
    type: str = "SOP"  # POLICY, SOP, FAQ
    dept: str = "Engineering"

class AssignRequest(BaseModel):
    dept: str

@router.get("/pending-drafts")
async def list_pending_drafts(
    status: str | None = Query(None),
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.list_drafts(status)

@router.post("/pending-drafts/{id}/approve")
async def approve_draft(
    id: uuid.UUID,
    req: ApproveRequest,
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.approve_draft(
        user=current_user,
        draft_id=id,
        category=req.type,
        dept=req.dept
    )

@router.post("/pending-drafts/{id}/reject")
async def reject_draft(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.reject_draft(current_user, id)

@router.get("/gaps")
async def list_search_gaps(
    status: str | None = Query(None),
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.list_gaps(status)

@router.post("/gaps/{id}/assign")
async def assign_gap(
    id: uuid.UUID,
    req: AssignRequest,
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.assign_gap(current_user, id, req.dept)

@router.post("/gaps/{id}/dismiss")
async def dismiss_gap(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.dismiss_gap(current_user, id)

@router.get("/audit-log")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(require_role(["Admin"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.list_audit_logs(current_user, limit)

@router.get("/health-metrics")
async def get_health_metrics(
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.get_dashboard_metrics(current_user)

@router.get("/eval-runs")
async def get_eval_runs(
    current_user: User = Depends(require_role(["Admin", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    ops_repo = OpsRepository(db)
    return await ops_repo.list_eval_runs()
