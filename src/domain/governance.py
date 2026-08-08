import asyncio
import uuid
from datetime import datetime
from typing import Sequence
from fastapi import HTTPException
from src.models.governance import PendingDraft, IngestionFingerprint, Gap, AuditLog
from src.models.article import Article, ArticleTag, ArticleVersion, DocumentSource
from src.models.user import User, AccessGroup, Department
from src.repositories.user import UserRepository
from src.models.connectors import ExternalDocument, ExternalGroupMapping
from src.models.ops import Connector, NotificationQueue
from sqlalchemy import delete, select
from src.repositories.governance import GovernanceRepository
from src.repositories.article import ArticleRepository
from src.domain.events import event_bus
from src.domain.content_restructure import restructure_document
from src.domain.rbac import AuthorizationService
from src.domain.source_storage import delete_source
from src.domain.departments import resolve_active_department, resolve_active_departments

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

    def _has_draft_permission(self, user: User, key: str, draft: PendingDraft) -> bool:
        """Evaluate a role scope against the draft's tenant and department."""
        return any(
            AuthorizationService.has_permission(user, key, draft, scope)
            for scope in ("own", "department", "company", "global")
        )

    def _can_review_draft(self, user: User, draft: PendingDraft) -> bool:
        return self._has_draft_permission(user, "article.review", draft) or self._has_draft_permission(user, "article.publish", draft)

    def _can_assign_approver(self, user: User, draft: PendingDraft) -> bool:
        # Assignment is a publishing/governance responsibility. A reviewer
        # may review an assigned draft but must not self-assign from the queue.
        return any(
            AuthorizationService.has_permission(user, "article.publish", draft, scope)
            for scope in ("department", "company", "global")
        )

    def _is_global_publisher(self, user: User) -> bool:
        return AuthorizationService.has_permission(user, "article.publish", requested_scope="global")

    async def list_drafts(self, user: User, status: str | None = None) -> Sequence[PendingDraft]:
        if self._is_global_publisher(user):
            return await self.gov_repo.list_drafts(status)
        can_company_review = any(
            AuthorizationService.has_permission(user, key, requested_scope="company")
            for key in ("article.review", "article.publish", "governance.read")
        )
        if can_company_review:
            return await self.gov_repo.list_drafts(status, user.company_domain)
        can_department_review = any(
            AuthorizationService.has_permission(user, key, requested_scope="department")
            for key in ("article.review", "article.publish", "governance.read")
        )
        owned_departments = AuthorizationService.owned_department_names(user)
        if can_department_review and owned_departments:
            return await self.gov_repo.list_drafts(status, user.company_domain, depts=owned_departments)
        raise HTTPException(status_code=403, detail="Not authorized to view the approval queue")

    async def assign_approver(self, user: User, draft_id: uuid.UUID, approver_id: uuid.UUID) -> PendingDraft:
        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "pending":
            raise HTTPException(status_code=409, detail="Only pending drafts can be assigned")
        if draft.company_domain != user.company_domain and not self._is_global_publisher(user):
            raise HTTPException(status_code=403, detail="Draft is outside your company")
        if not self._can_assign_approver(user, draft):
            raise HTTPException(status_code=403, detail="Not authorized to assign approvers for this draft")

        approver = await UserRepository(self.gov_repo.db).get_by_id(approver_id)
        if not approver or not approver.active or (approver.company_domain != draft.company_domain):
            raise HTTPException(status_code=422, detail="Approver must be an active user in the draft's company")
        if draft.created_by and approver.id == draft.created_by:
            raise HTTPException(status_code=422, detail="A submitter cannot approve their own draft")
        if not self._can_review_draft(approver, draft):
            raise HTTPException(status_code=422, detail="Selected user does not have approval permission for this draft")

        draft.assigned_approver_id = approver.id
        draft.assigned_by = user.id
        draft.assigned_at = datetime.utcnow()
        updated = await self.gov_repo.update_draft(draft)
        self.gov_repo.db.add(NotificationQueue(
            recipient_user_id=approver.id,
            type="in_app",
            payload={"event": "draft_assigned", "draft_id": str(draft.id), "approver_id": str(approver.id), "assigned_by": str(user.id)},
        ))
        await self.gov_repo.db.commit()
        await self.log_audit(user.id, "assign_approver", "draft", str(draft.id))
        return updated

    async def eligible_approvers(self, user: User, draft_id: uuid.UUID) -> Sequence[User]:
        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.company_domain != user.company_domain and not self._is_global_publisher(user):
            raise HTTPException(status_code=403, detail="Draft is outside your company")
        if not self._can_assign_approver(user, draft):
            raise HTTPException(status_code=403, detail="Not authorized to assign approvers for this draft")
        users = await UserRepository(self.gov_repo.db).list_users(limit=500)
        return [candidate for candidate in users if candidate.active and candidate.company_domain == draft.company_domain and candidate.id != draft.created_by and self._can_review_draft(candidate, draft)]

    async def approve_draft(self, user: User, draft_id: uuid.UUID, category: str | None = None, dept: str | None = None, update_article_id: uuid.UUID | None = None, treat_as_new: bool = False, sensitivity: str | None = None, access_group_ids: list[uuid.UUID] | None = None, review_note: str | None = None) -> Article:
        """Publish one draft atomically.

        The lock is deliberately taken before validation.  A second request
        waits, then sees the committed ``approved`` state instead of creating
        a second article from the same draft.
        """
        db = self.gov_repo.db
        if hasattr(db, "execute"):
            draft = (await db.execute(
                select(PendingDraft).where(PendingDraft.id == draft_id).with_for_update()
            )).scalar_one_or_none()
        else:  # Lightweight unit-test repository compatibility.
            draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        try:
            metadata = getattr(draft, "content_metadata", None) or {}
            category = category or str(metadata.get("type") or "SOP")
            dept = dept or str(metadata.get("dept") or draft.dept or "")
            sensitivity = sensitivity or str(metadata.get("sensitivity") or "public")
            if access_group_ids is None:
                try:
                    access_group_ids = [uuid.UUID(str(group_id)) for group_id in metadata.get("access_group_ids", [])]
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=422, detail="The submitted access-group selection is invalid") from exc
            if draft.status != "pending":
                raise HTTPException(status_code=409, detail=f"Draft cannot be approved from status: {draft.status}")
            if draft.company_domain != user.company_domain and not self._is_global_publisher(user):
                raise HTTPException(status_code=403, detail="Draft is outside your company")
            if not draft.assigned_approver_id:
                raise HTTPException(status_code=409, detail="Assign an approver before publishing this draft")
            if draft.assigned_approver_id != user.id or not self._can_review_draft(user, draft):
                raise HTTPException(status_code=403, detail="This draft is assigned to another approver")
            if draft.requires_update_confirmation and not update_article_id and not treat_as_new:
                raise HTTPException(status_code=409, detail={"code": "update_confirmation_required", "matches": draft.similarity_matches or []})
            if metadata.get("submission_kind") == "manual_update":
                expected_target = str(metadata.get("suggested_update_article_id") or "")
                if treat_as_new or not update_article_id or str(update_article_id) != expected_target:
                    raise HTTPException(status_code=422, detail="A manually submitted article change must update its original article")
            if not dept.strip():
                raise HTTPException(status_code=422, detail="An active department is required before publishing")
            dept = (await resolve_active_department(db, draft.company_domain, dept)).name
            if draft.dept and dept != draft.dept:
                raise HTTPException(status_code=422, detail="An approved article must remain in the submitted department")
            try:
                department_ids = [uuid.UUID(str(item)) for item in metadata.get("department_ids", [])]
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="The submitted department selection is invalid") from exc
            if department_ids:
                selected_departments = await resolve_active_departments(db, draft.company_domain, department_ids)
                if dept not in {department.name for department in selected_departments}:
                    raise HTTPException(status_code=422, detail="The submitted primary department must be selected")
            else:
                selected_departments = [await resolve_active_department(db, draft.company_domain, dept)]
            if category not in {"POLICY", "SOP", "DECISION", "FAQ", "RCA", "HOWTO", "PLAYBOOK", "REFERENCE"}:
                raise HTTPException(status_code=422, detail="Invalid article type")
            if sensitivity not in {"public", "internal", "confidential", "restricted"}:
                raise HTTPException(status_code=422, detail="Invalid article sensitivity")

            selected_groups: list[AccessGroup] = []
            if access_group_ids:
                selected_groups = list(await UserRepository(db).get_groups_by_ids(access_group_ids, draft.company_domain))
                if len({group.id for group in selected_groups}) != len(set(access_group_ids)):
                    raise HTTPException(status_code=422, detail="One or more access groups do not exist")
            # New content is authorized by role/permission/department. If a
            # legacy draft carries a non-public flag without a real ACL, make
            # it compatible with the current resource-based model instead of
            # blocking publication on a removed UI control.
            if sensitivity != "public" and not selected_groups:
                sensitivity = "public"
            external_document = await db.get(ExternalDocument, draft.external_document_id) if draft.external_document_id else None
            if external_document:
                mapped_ids = (external_document.metadata_json or {}).get("mapped_access_group_ids", [])
                selected_groups = list((await db.execute(select(AccessGroup).where(AccessGroup.id.in_(mapped_ids), AccessGroup.company_domain == draft.company_domain))).scalars().all()) if mapped_ids else []
                if len({group.id for group in selected_groups}) != len(set(mapped_ids)):
                    raise HTTPException(status_code=422, detail="The connector ACL contains an invalid access group")
                if sensitivity != "public" and not selected_groups:
                    raise HTTPException(status_code=422, detail="A non-public connector document requires a mapped access group")
                connector = await db.get(Connector, external_document.connector_id)
                if not connector or connector.company_domain != draft.company_domain:
                    raise HTTPException(status_code=422, detail="The connector document is outside this draft's company")

            update_target = None
            if update_article_id:
                update_target = await self.article_repo.get_by_id_for_update(update_article_id) if hasattr(self.article_repo, "get_by_id_for_update") else await self.article_repo.get_by_id(update_article_id)
                if not update_target or (update_target.company_domain != user.company_domain and not AuthorizationService.has_permission(user, "article.read", requested_scope="global")):
                    raise HTTPException(status_code=403, detail="The selected update target is not accessible")
                from src.domain.permissions import PermissionService
                if not PermissionService.can_view_article(user, update_target) or not any(AuthorizationService.has_permission(user, "article.edit", update_target, scope) or AuthorizationService.has_permission(user, "article.publish", update_target, scope) for scope in ("own", "department", "company", "global")):
                    raise HTTPException(status_code=403, detail="Not authorized to supersede the selected article")
                if update_target.lifecycle_status != "active":
                    raise HTTPException(status_code=409, detail="The selected update target is already inactive")

            requested_external_id = str(metadata.get("external_id") or "").strip() or (update_target.external_id if update_target else None)
            if requested_external_id:
                external_stmt = select(Article).where(
                    Article.company_domain == draft.company_domain,
                    Article.external_id == requested_external_id,
                )
                if update_target:
                    external_stmt = external_stmt.where(Article.id != update_target.id)
                if await db.scalar(external_stmt):
                    raise HTTPException(status_code=409, detail="An article with this external ID already exists in this company")

            next_review = None
            if metadata.get("next_review"):
                try:
                    next_review = datetime.fromisoformat(str(metadata["next_review"]).replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail="The submitted review date is invalid") from exc
            elif update_target:
                next_review = update_target.next_review
            next_version = (update_target.version + 1) if update_target else 1
            body_md = draft.restructured_body_md or draft.summary or f"Draft imported from {draft.source_ref}. Content pending edit."
            draft_tags = list(dict.fromkeys(str(tag).strip() for tag in (draft.tags or []) if str(tag).strip()))[:20]
            if update_target and draft.tags is None:
                draft_tags = [tag.tag for tag in update_target.tags]
            if update_target:
                # Revisions keep the stable article identity. Replacing the
                # row previously broke bookmarks, comments, external links,
                # ACL references, and the article's version history.
                created_article = update_target
                created_article.title = draft.title
                created_article.body_md = body_md
                created_article.external_id = requested_external_id
                created_article.dept = dept
                created_article.departments = selected_departments
                created_article.domain = str(metadata.get("domain") or created_article.domain)
                created_article.type = category
                created_article.sensitivity = sensitivity
                created_article.language = str(metadata.get("language") or created_article.language)
                created_article.next_review = next_review
                created_article.status = "published"
                created_article.lifecycle_status = "active"
                created_article.version = next_version
                created_article.last_reviewed = datetime.utcnow()
                created_article.index_status = "pending"
                created_article.index_error = None
                created_article.related_article_ids = draft.related_article_ids if draft.related_article_ids is not None else created_article.related_article_ids
                created_article.access_groups = selected_groups
                draft.update_target_article_id = created_article.id
                await db.execute(delete(ArticleTag).where(ArticleTag.article_id == created_article.id))
                db.add(AuditLog(user_id=user.id, action="update", target_type="article", target_id=str(created_article.id)))
            else:
                created_article = Article(
                    title=draft.title, body_md=body_md,
                    external_id=requested_external_id, dept=dept, domain=str(metadata.get("domain") or "Ingestion"), type=category,
                    sensitivity=sensitivity, language=str(metadata.get("language") or "en"), next_review=next_review, owner_id=user.id,
                    status="published", version=next_version, company_domain=draft.company_domain,
                    lifecycle_status="active", last_reviewed=datetime.utcnow(), related_article_ids=draft.related_article_ids, access_groups=selected_groups,
                    departments=selected_departments,
                )
                db.add(created_article)
                await db.flush()
                db.add(AuditLog(user_id=user.id, action="create", target_type="article", target_id=str(created_article.id)))
            db.add_all(ArticleTag(article_id=created_article.id, tag=tag) for tag in draft_tags)
            db.add(ArticleVersion(article_id=created_article.id, version=next_version, snapshot={
                "title": created_article.title, "body_md": created_article.body_md, "dept": created_article.dept,
                "department_ids": [str(department.id) for department in selected_departments],
                "domain": created_article.domain, "type": created_article.type, "sensitivity": created_article.sensitivity,
                "language": created_article.language, "external_id": created_article.external_id, "tags": draft_tags,
            }, edited_by=user.id))
            if draft.storage_key:
                db.add(DocumentSource(article_id=created_article.id, source_system=connector.system if external_document else "upload", source_ref=draft.source_ref, source_hash=draft.source_hash, storage_key=draft.storage_key, original_filename=draft.original_filename or draft.title, mime_type=draft.mime_type, page_texts=draft.page_texts))
            if external_document:
                external_document.article_id = created_article.id
            fingerprint = await db.scalar(select(IngestionFingerprint).where(
                IngestionFingerprint.company_domain == draft.company_domain,
                IngestionFingerprint.source_hash == draft.source_hash,
            ))
            if fingerprint:
                fingerprint.status = "approved"
                fingerprint.article_id = created_article.id
            else:
                db.add(IngestionFingerprint(company_domain=draft.company_domain, source_hash=draft.source_hash, status="approved", article_id=created_article.id, created_by=draft.created_by))
            draft.status = "approved"
            draft.reviewed_by = user.id
            draft.reviewed_at = datetime.utcnow()
            draft.review_note = review_note.strip() if review_note and review_note.strip() else None
            db.add_all([
                AuditLog(user_id=user.id, action="approve", target_type="draft", target_id=str(draft.id)),
                AuditLog(user_id=user.id, action="publish", target_type="article", target_id=str(created_article.id)),
            ])
            if draft.created_by and draft.created_by != user.id:
                db.add(NotificationQueue(
                    recipient_user_id=draft.created_by,
                    type="in_app",
                    payload={"event": "draft_approved", "draft_id": str(draft.id), "article_id": str(created_article.id), "reviewer_id": str(user.id)},
                ))
            await db.commit()
        except Exception:
            if hasattr(db, "rollback"):
                await db.rollback()
            raise

        published = await self.article_repo.get_by_id(created_article.id)
        if not published:
            raise HTTPException(status_code=500, detail="Published article could not be reloaded")
        await event_bus.publish("ArticleUpdated" if update_target else "ArticlePublished", {"article_id": str(published.id)})
        return published

    async def restructure_draft(self, user: User, draft_id: uuid.UUID, enabled: bool = True) -> PendingDraft:
        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending drafts can be restructured")
        if draft.company_domain != user.company_domain and not self._is_global_publisher(user):
            raise HTTPException(status_code=403, detail="Draft is outside your company")
        if draft.assigned_approver_id != user.id or not self._can_review_draft(user, draft):
            raise HTTPException(status_code=403, detail="Only the assigned approver can restructure this draft")

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

    async def reject_draft(self, user: User, draft_id: uuid.UUID, review_note: str) -> PendingDraft:
        draft = await self.gov_repo.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")

        if draft.status != "pending":
            raise HTTPException(status_code=400, detail=f"Draft cannot be rejected from status: {draft.status}")
        if draft.company_domain != user.company_domain and not self._is_global_publisher(user):
            raise HTTPException(status_code=403, detail="Draft is outside your company")
        if not draft.assigned_approver_id:
            raise HTTPException(status_code=409, detail="Assign an approver before rejecting this draft")
        if draft.assigned_approver_id != user.id or not self._can_review_draft(user, draft):
            raise HTTPException(status_code=403, detail="This draft is assigned to another approver")

        draft.status = "rejected"
        draft.reviewed_by = user.id
        draft.reviewed_at = datetime.utcnow()
        draft.review_note = review_note.strip()
        storage_key = draft.storage_key if not draft.external_document_id else None
        if hasattr(self.gov_repo.db, "execute"):
            await self.gov_repo.db.execute(delete(IngestionFingerprint).where(
                IngestionFingerprint.company_domain == draft.company_domain,
                IngestionFingerprint.source_hash == draft.source_hash,
            ))
        self.gov_repo.db.add(NotificationQueue(
            recipient_user_id=draft.created_by,
            type="in_app",
            payload={"event": "draft_rejected", "draft_id": str(draft.id), "created_by": str(draft.created_by) if draft.created_by else None, "reviewer_id": str(user.id)},
        ))
        updated_draft = await self.gov_repo.update_draft(draft)
        if storage_key:
            try:
                await asyncio.to_thread(delete_source, storage_key)
            except Exception:
                # Rejection must remain successful; an operator/GC job can
                # remove an unavailable object-store key later.
                pass

        # Log Audit Trail
        await self.log_audit(
            user_id=user.id,
            action="reject",
            target_type="draft",
            target_id=str(draft.id)
        )

        return updated_draft

    # Gap Queue
    async def list_gaps(self, user: User, status: str | None = None) -> Sequence[Gap]:
        if not AuthorizationService.has_permission(user, "governance.read", requested_scope="company"):
            raise HTTPException(status_code=403, detail="Not authorized to view gaps")
        company_domain = None if AuthorizationService.has_permission(user, "governance.read", requested_scope="global") else user.company_domain
        return await self.gov_repo.list_gaps(status, company_domain)

    async def assign_gap(self, user: User, gap_id: uuid.UUID, dept: str) -> Gap:
        if not AuthorizationService.has_permission(user, "governance.read", requested_scope="company"):
            raise HTTPException(status_code=403, detail="Not authorized to manage gaps")

        company_domain = None if AuthorizationService.has_permission(user, "governance.read", requested_scope="global") else user.company_domain
        gap = await self.gov_repo.get_gap(gap_id, company_domain)
        if not gap:
            raise HTTPException(status_code=404, detail="Gap not found")

        gap.dept = (await resolve_active_department(self.gov_repo.db, gap.company_domain, dept)).name
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
        if not AuthorizationService.has_permission(user, "governance.read", requested_scope="company"):
            raise HTTPException(status_code=403, detail="Not authorized to manage gaps")

        company_domain = None if AuthorizationService.has_permission(user, "governance.read", requested_scope="global") else user.company_domain
        gap = await self.gov_repo.get_gap(gap_id, company_domain)
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
        if not AuthorizationService.has_permission(user, "governance.read", requested_scope="global"):
            raise HTTPException(status_code=403, detail="Only global administrators can access cross-company metrics")
        return await self.gov_repo.get_health_metrics()

    async def list_audit_logs(self, user: User, limit: int = 100) -> Sequence[AuditLog]:
        if not AuthorizationService.has_permission(user, "governance.read", requested_scope="global"):
            raise HTTPException(status_code=403, detail="Only Admins can view full audit logs")
        return await self.gov_repo.list_audits(limit=limit)
