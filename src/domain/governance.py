import uuid
from typing import Sequence
from fastapi import HTTPException
from src.models.governance import PendingDraft, Gap, AuditLog
from src.models.article import Article, ArticleVersion, DocumentSource
from src.models.user import User
from src.repositories.governance import GovernanceRepository
from src.repositories.article import ArticleRepository
from src.domain.events import event_bus
from src.domain.content_restructure import restructure_document

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

    async def approve_draft(self, user: User, draft_id: uuid.UUID, category: str = "SOP", dept: str = "Engineering", update_article_id: uuid.UUID | None = None, treat_as_new: bool = False) -> Article:
        if user.role not in ["Admin", "CEO", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to approve drafts")

        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft cannot be approved from status: {draft.status}")
        if draft.requires_update_confirmation and not update_article_id and not treat_as_new:
            raise HTTPException(status_code=409, detail={"code": "update_confirmation_required", "matches": draft.similarity_matches or []})

        update_target = None
        if update_article_id:
            update_target = await self.article_repo.get_by_id(update_article_id)
            if not update_target or update_target.company_domain != user.company_domain and user.role != "Admin":
                raise HTTPException(status_code=403, detail="The selected update target is not accessible")
            if update_target.lifecycle_status != "active":
                raise HTTPException(status_code=409, detail="The selected update target is already inactive")

        next_version = (update_target.version + 1) if update_target else 1

        # 1. Create a new Article based on draft details
        # Access groups will defaults to public (bit 0)
        article = Article(
            title=draft.title,
            body_md=draft.restructured_body_md or draft.summary or f"Draft imported from {draft.source_ref}. Content pending edit.",
            dept=dept,
            domain="Ingestion",
            type=category,
            sensitivity="internal",
            owner_id=user.id,
            status="published",
            version=next_version,
            company_domain=user.company_domain,
            lifecycle_status="active",
            related_article_ids=draft.related_article_ids,
        )
        created_article = await self.article_repo.create(article)

        draft_tags = [str(tag).strip() for tag in (draft.tags or []) if str(tag).strip()][:20]
        if draft_tags:
            await self.article_repo.sync_tags(created_article.id, draft_tags)
            created_article = await self.article_repo.get_by_id(created_article.id)
            if not created_article:
                raise HTTPException(status_code=500, detail="Failed to load the newly published article")

        await self.article_repo.create_version(ArticleVersion(
            article_id=created_article.id,
                version=next_version,
            snapshot={
                "title": created_article.title,
                "body_md": created_article.body_md,
                "dept": created_article.dept,
                "domain": created_article.domain,
                "type": created_article.type,
                "sensitivity": created_article.sensitivity,
                "language": created_article.language,
                "tags": draft_tags,
            },
            edited_by=user.id,
        ))

        if draft.storage_key:
            self.gov_repo.db.add(DocumentSource(
                article_id=created_article.id,
                source_system="upload",
                source_ref=draft.source_ref,
                source_hash=draft.source_hash,
                storage_key=draft.storage_key,
                original_filename=draft.original_filename or draft.title,
                mime_type=draft.mime_type,
                page_texts=draft.page_texts,
            ))
            await self.gov_repo.db.commit()

        if update_target:
            update_target.lifecycle_status = "superseded"
            update_target.status = "archived"
            await self.article_repo.update(update_target)
            await self.log_audit(user.id, "supersede", "article", str(update_target.id))
            draft.update_target_article_id = update_target.id

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
        await self.log_audit(
            user_id=user.id,
            action="publish",
            target_type="article",
            target_id=str(created_article.id)
        )

        # 4. Emit ArticlePublished event
        await event_bus.publish("ArticlePublished", {"article_id": str(created_article.id)})

        return created_article

    async def restructure_draft(self, user: User, draft_id: uuid.UUID, enabled: bool = True) -> PendingDraft:
        if user.role not in ["Admin", "CEO", "Reviewer", "Department Owner"]:
            raise HTTPException(status_code=403, detail="Not authorized to restructure drafts")
        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending drafts can be restructured")

        source_text = draft.summary or "\n\n".join(
            str(page.get("text", "")) for page in (draft.page_texts or []) if page.get("text")
        )
        result = await restructure_document(draft.title, source_text, enabled=enabled)
        draft.restructured_body_md = result.body_md
        draft.restructure_status = result.status
        draft.restructure_model = result.model
        draft.restructure_error = result.error
        updated = await self.gov_repo.update_draft(draft)
        await self.log_audit(user.id, "restructure", "draft", str(draft.id))
        return updated

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
