"""Demo-parity knowledge surfaces built on the existing article/governance models."""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db, get_current_user, require_role
from src.models import User
from src.models.article import Article
from src.models.governance import Gap, PendingDraft
from src.models.user import AccessGroup
from src.repositories.article import ArticleRepository
from src.repositories.governance import GovernanceRepository

router = APIRouter()

TEMPLATES: dict[str, dict[str, Any]] = {
    "POLICY": {"name": "Policy", "description": "Rules, boundaries, and ownership.", "sections": ["Purpose", "Scope", "Policy", "Responsibilities", "Exceptions", "Review and approval"]},
    "SOP": {"name": "Standard operating procedure", "description": "Repeatable operational process.", "sections": ["Purpose", "Prerequisites", "Procedure", "Verification", "Rollback or escalation"]},
    "DECISION": {"name": "Decision record", "description": "A durable record of a technical or business decision.", "sections": ["Context", "Options considered", "Decision and rationale", "Consequences", "Owners and follow-up"]},
    "FAQ": {"name": "FAQ", "description": "Canonical answers to recurring questions.", "sections": ["Question", "Answer", "Related resources"]},
    "RCA": {"name": "Root cause analysis", "description": "Incident impact, causes, and corrective actions.", "sections": ["Incident summary", "Impact", "Timeline", "Root cause", "Corrective actions"]},
    "HOWTO": {"name": "How-to", "description": "Focused task instructions.", "sections": ["When to use this", "Steps", "Troubleshooting"]},
    "PLAYBOOK": {"name": "Playbook", "description": "A response workflow with roles and exit criteria.", "sections": ["Trigger", "Roles", "Response steps", "Exit criteria"]},
    "REFERENCE": {"name": "Reference", "description": "Stable reference material and examples.", "sections": ["Summary", "Details", "Examples", "Related links"]},
}


class ContentRequest(BaseModel):
    query: str = Field(min_length=2, max_length=255)
    dept: str | None = Field(default=None, max_length=100)


class RolePreviewRequest(BaseModel):
    role: str = Field(min_length=2, max_length=50)
    dept: str | None = Field(default=None, max_length=100)


def _article_card(article: Article) -> dict[str, Any]:
    return {
        "id": str(article.id), "title": article.title, "dept": article.dept,
        "external_id": article.external_id,
        "domain": article.domain, "type": article.type, "status": article.status,
        "sensitivity": article.sensitivity, "language": article.language,
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
    pending = await db.scalar(select(func.count()).select_from(PendingDraft).where(PendingDraft.status == "pending")) or 0
    gaps = await db.scalar(select(func.count()).select_from(Gap).where(Gap.status.in_(["open", "assigned"]))) or 0
    return {
        "total_articles": len(articles), "departments": len({article.dept for article in articles}),
        "domains": len({(article.dept, article.domain) for article in articles}),
        "with_owner_percent": round(sum(bool(article.owner_id) for article in articles) / len(articles) * 100) if articles else 0,
        "needs_review": sum(_article_card(article)["needs_update"] for article in articles),
        "pending_drafts": pending, "open_gaps": gaps,
        "recent": [_article_card(article) for article in articles[:6]],
    }


@router.get("/browse")
async def browse_knowledge(
    dept: str | None = Query(None), domain: str | None = Query(None),
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    articles = list(await ArticleRepository(db).list_articles(current_user, dept=dept, status="published"))
    if domain:
        articles = [article for article in articles if article.domain == domain]
    tree: dict[str, dict[str, int]] = {}
    for article in articles:
        domains = tree.setdefault(article.dept, {})
        domains[article.domain] = domains.get(article.domain, 0) + 1
    return {"taxonomy": tree, "articles": [_article_card(article) for article in articles]}


@router.get("/templates")
async def templates(current_user: User = Depends(get_current_user)) -> dict[str, dict[str, Any]]:
    return TEMPLATES


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


@router.get("/permissions")
async def permissions(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    groups = (await db.execute(select(AccessGroup).order_by(AccessGroup.bitmask_position))).scalars().all()
    articles = list(await ArticleRepository(db).list_articles(current_user))
    return {"current_user": {"id": str(current_user.id), "name": current_user.name, "role": current_user.role, "dept": current_user.dept},
            "groups": [{"id": str(group.id), "name": group.name, "bitmask_position": group.bitmask_position,
                        "member": any(item.id == group.id for item in current_user.groups)} for group in groups],
            "visible_article_count": len(articles),
            "restricted_count": sum(article.sensitivity in {"confidential", "restricted"} for article in articles)}


@router.post("/content-requests", status_code=201)
async def content_request(request: ContentRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    gap = await GovernanceRepository(db).log_gap(request.query.strip(), request.dept)
    return {"id": str(gap.id), "query": gap.query, "dept": gap.dept, "count": gap.count, "status": gap.status}


@router.post("/role-preview")
async def role_preview(request: RolePreviewRequest, current_user: User = Depends(require_role(["Admin"])), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    allowed = {"Admin", "CEO", "Department Owner", "Reviewer", "Staff"}
    if request.role not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported role preview")
    from src.repositories.audit import AuditRepository
    await AuditRepository(db).record(current_user.id, "role_preview", "user", str(current_user.id))
    return {"role": request.role, "dept": request.dept, "read_only": True,
            "message": "Preview metadata only; authenticated-user permissions remain authoritative."}
