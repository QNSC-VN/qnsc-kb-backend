import uuid
from typing import Sequence
from fastapi import HTTPException
from src.models.governance import PendingDraft, Gap, AuditLog
from src.models.article import Article
from src.models.user import User
from src.repositories.governance import GovernanceRepository
from src.repositories.article import ArticleRepository
from src.domain.events import event_bus

class GovernanceService:
    def __init__(self, gov_repo: GovernanceRepository, article_repo: ArticleRepository):
        self.gov_repo = gov_repo
        self.article_repo = article_repo

    async def log_audit(self, user_id: uuid.UUID | None, action: str, target_type: str, target_id: str | None = None) -> AuditLog:
        log = AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id
        )
        return await self.gov_repo.log_audit(log)

    async def list_drafts(self, status: str | None = None) -> Sequence[PendingDraft]:
        return await self.gov_repo.list_drafts(status)

    async def approve_draft(self, user: User, draft_id: uuid.UUID, category: str = "SOP", dept: str = "Engineering") -> Article:
        if user.role not in ["Admin", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to approve drafts")

        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft cannot be approved from status: {draft.status}")

        # 1. Create a new Article based on draft details
        # Access groups will defaults to public (bit 0)
        article = Article(
            title=draft.title,
            body_md=draft.summary or f"Draft imported from {draft.source_ref}. Content pending edit.",
            dept=dept,
            domain="Ingestion",
            type=category,
            sensitivity="internal",
            owner_id=user.id,
            status="published",
            version=1
        )
        created_article = await self.article_repo.create(article)

        # 2. Update Draft Status
        draft.status = "approved"
        await self.gov_repo.update_draft(draft)

        # 3. Log Audit Trail
        await self.log_audit(
            user_id=user.id,
            action="approve",
            target_type="draft",
            target_id=str(draft.id)
        )
        
        await self.log_audit(
            user_id=user.id,
            action="create",
            target_type="article",
            target_id=str(created_article.id)
        )

        # 4. Emit ArticlePublished event
        await event_bus.publish("ArticlePublished", {"article_id": str(created_article.id)})

        return created_article

    async def reject_draft(self, user: User, draft_id: uuid.UUID) -> PendingDraft:
        if user.role not in ["Admin", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to reject drafts")

        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft cannot be rejected from status: {draft.status}")

        draft.status = "rejected"
        updated_draft = await self.gov_repo.update_draft(draft)

        # Log Audit Trail
        await self.log_audit(
            user_id=user.id,
            action="reject",
            target_type="draft",
            target_id=str(draft.id)
        )

        return updated_draft

    # Gap Queue
    async def list_gaps(self, status: str | None = None) -> Sequence[Gap]:
        return await self.gov_repo.list_gaps(status)

    async def assign_gap(self, user: User, gap_id: uuid.UUID, dept: str) -> Gap:
        if user.role not in ["Admin", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to manage gaps")

        gap = await self.gov_repo.get_gap(gap_id)
        if not gap:
            raise HTTPException(status_code=404, detail="Gap not found")

        gap.dept = dept
        gap.status = "assigned"
        updated_gap = await self.gov_repo.update_gap(gap)

        # Log Audit Trail
        await self.log_audit(
            user_id=user.id,
            action="assign",
            target_type="gap",
            target_id=str(gap.id)
        )

        return updated_gap

    async def dismiss_gap(self, user: User, gap_id: uuid.UUID) -> Gap:
        if user.role not in ["Admin", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to manage gaps")

        gap = await self.gov_repo.get_gap(gap_id)
        if not gap:
            raise HTTPException(status_code=404, detail="Gap not found")

        gap.status = "dismissed"
        updated_gap = await self.gov_repo.update_gap(gap)

        # Log Audit Trail
        await self.log_audit(
            user_id=user.id,
            action="dismiss",
            target_type="gap",
            target_id=str(gap.id)
        )

        return updated_gap

    # Dashboard Metrics
    async def get_dashboard_metrics(self, user: User) -> dict:
        # Enforced check for Admin or review roles
        if user.role not in ["Admin", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to access metrics dashboard")
        return await self.gov_repo.get_health_metrics()

    async def list_audit_logs(self, user: User, limit: int = 100) -> Sequence[AuditLog]:
        if user.role != "Admin":
            raise HTTPException(status_code=403, detail="Only Admins can view full audit logs")
        return await self.gov_repo.list_audits(limit=limit)
