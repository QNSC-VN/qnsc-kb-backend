"""Knowledge surfaces backed by the live article and governance models."""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db, get_current_user, require_permission
from src.models import User
from src.models.article import Article
from src.models.governance import Gap, PendingDraft
from src.repositories.article import ArticleRepository
from src.repositories.governance import GovernanceRepository
from src.domain.rbac import AuthorizationService

router = APIRouter()

class ContentRequest(BaseModel):
    query: str = Field(min_length=2, max_length=255)
    dept: str | None = Field(default=None, max_length=100)


class RolePreviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=50)
    dept: str | None = Field(default=None, max_length=100)


def _article_card(article: Article) -> dict[str, Any]:
    return {
        "id": str(article.id), "title": article.title, "dept": article.dept,
        "departments": [{"id": str(department.id), "name": department.name} for department in getattr(article, "departments", [])],
        "external_id": article.external_id, "status": article.status,
        "language": article.language,
        "version": article.version, "owner": article.owner.name if article.owner else None,
        "owner_id": str(article.owner_id) if article.owner_id else None,
        "tags": [tag.tag for tag in article.tags], "related_article_ids": article.related_article_ids or [],
        "next_review": article.next_review, "last_reviewed": article.last_reviewed,
        "needs_update": article.needs_update or bool(article.next_review and article.next_review < datetime.utcnow()),
        "source_count": len(article.sources),
    }


@router.get("/home")
async def home_summary(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    articles = list(await ArticleRepository(db).list_articles(current_user, status="published"))
    home_has_full_company_access = (
        AuthorizationService.has_permission(current_user, "governance.read", requested_scope="global")
        or AuthorizationService.has_full_company_article_access(current_user)
    )
    home_departments = AuthorizationService.member_department_names(current_user)
    gaps_stmt = select(func.count()).select_from(Gap).where(
        Gap.status.in_(["open", "assigned"]),
        Gap.company_domain == current_user.company_domain,
    )
    if not home_has_full_company_access:
        gaps_stmt = gaps_stmt.where(Gap.dept.in_(home_departments))
    pending = await GovernanceRepository(db).count_pending_for_user(current_user)
    gaps = await db.scalar(gaps_stmt) or 0
    return {
        "total_articles": len(articles), "departments": len({department.name for article in articles for department in (article.departments or [])} | {article.dept for article in articles}),
        "with_owner_percent": round(sum(bool(article.owner_id) for article in articles) / len(articles) * 100) if articles else 0,
        "needs_review": sum(_article_card(article)["needs_update"] for article in articles),
        "pending_drafts": pending, "open_gaps": gaps,
        "recent": [_article_card(article) for article in articles[:6]],
    }


@router.get("/browse")
async def browse_knowledge(
    dept: str | None = Query(None),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    articles = list(await ArticleRepository(db).list_articles(current_user, dept=dept, status="published"))
    return {"articles": [_article_card(article) for article in articles]}


@router.get("/sources")
async def sources(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    articles = list(await ArticleRepository(db).list_articles(current_user))
    result = []
    for article in articles:
        for source in article.sources:
            result.append({"id": str(source.id), "article_id": str(article.id), "article_title": article.title,
                           "source_system": source.source_system, "source_ref": source.source_ref,
                           "filename": source.original_filename, "mime_type": source.mime_type,
                           "ingested_at": source.ingested_at, "has_file": bool(source.storage_key)})
    return result


@router.post("/content-requests", status_code=201)
async def content_request(request: ContentRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    gap = await GovernanceRepository(db).log_gap(request.query.strip(), current_user.company_domain, request.dept)
    return {"id": str(gap.id), "query": gap.query, "dept": gap.dept, "count": gap.count, "status": gap.status}


@router.post("/role-preview")
async def role_preview(request: RolePreviewRequest, current_user: User = Depends(require_permission("permission.manage")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    allowed = {"Admin", "CEO", "Reviewer", "Staff"}
    if request.role not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported role preview")
    from src.repositories.audit import AuditRepository
    await AuditRepository(db).record(current_user.id, "role_preview", "user", str(current_user.id))
    return {"role": request.role, "dept": request.dept, "read_only": True,
            "message": "Preview metadata only; authenticated-user permissions remain authoritative."}
