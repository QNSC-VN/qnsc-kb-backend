import uuid
import structlog
from datetime import datetime
from typing import Sequence
from fastapi import HTTPException, status
from src.models.article import Article, ArticleVersion, ArticleTag
from src.models.user import User
from src.repositories.article import ArticleRepository
from src.repositories.user import UserRepository
from src.domain.permissions import PermissionService
from src.domain.events import event_bus
from src.repositories.audit import AuditRepository

logger = structlog.get_logger()

class ArticleService:
    def __init__(self, article_repo: ArticleRepository, user_repo: UserRepository, audit_repo: AuditRepository | None = None):
        self.article_repo = article_repo
        self.user_repo = user_repo
        self.audit_repo = audit_repo

    async def _audit(self, user_id: uuid.UUID, action: str, article_id: uuid.UUID) -> None:
        if self.audit_repo:
            await self.audit_repo.record(user_id, action, "article", str(article_id))

    async def create_article(
        self,
        user: User,
        title: str,
        body_md: str,
        dept: str,
        domain: str,
        type_: str,
        sensitivity: str,
        tags: list[str],
        language: str = "en",
        access_group_ids: list[uuid.UUID] | None = None,
        next_review: datetime | None = None,
        original_body_md: str | None = None,
    ) -> Article:
        # Resolve access groups
        groups = []
        if access_group_ids:
            groups = list(await self.user_repo.get_groups_by_ids(access_group_ids))

        # Default sensitivity and status logic:
        # Department owners/admins can publish directly, staff create drafts
        initial_status = "published" if user.role in ["Admin", "CEO", "Department Owner"] else "draft"

        article = Article(
            title=title,
            body_md=body_md,
            dept=dept,
            domain=domain,
            company_domain=user.company_domain,
            type=type_,
            sensitivity=sensitivity,
            language=language,
            owner_id=user.id,
            status=initial_status,
            version=1,
            next_review=next_review,
            last_reviewed=datetime.utcnow() if initial_status == "published" else None,
            access_groups=groups
        )

        created_article = await self.article_repo.create(article)
        logger.info(
            "Article created",
            article_id=str(created_article.id),
            status=created_article.status,
            owner_id=str(user.id),
            owner_role=user.role,
            sensitivity=created_article.sensitivity,
        )
        
        # Save tags
        if tags:
            await self.article_repo.sync_tags(created_article.id, tags)
            
        # Re-fetch article with tags
        updated_article = await self.article_repo.get_by_id(created_article.id)
        if not updated_article:
            raise HTTPException(status_code=500, detail="Failed to retrieve created article")

        # Save initial version snapshot
        snapshot = {
            "title": updated_article.title,
            "body_md": updated_article.body_md,
            "original_body_md": original_body_md or updated_article.body_md,
            "dept": updated_article.dept,
            "domain": updated_article.domain,
            "type": updated_article.type,
            "sensitivity": updated_article.sensitivity,
            "language": updated_article.language,
            "tags": tags
        }
        version = ArticleVersion(
            article_id=updated_article.id,
            version=1,
            snapshot=snapshot,
            edited_by=user.id
        )
        await self.article_repo.create_version(version)
        await self._audit(user.id, "create", updated_article.id)
        if updated_article.status == "published":
            await self._audit(user.id, "publish", updated_article.id)

        # Trigger event if published
        if updated_article.status == "published":
            logger.info("Published article indexing event queued", article_id=str(updated_article.id))
            await event_bus.publish("ArticlePublished", {"article_id": str(updated_article.id)})

        return updated_article

    async def update_article(
        self,
        user: User,
        article_id: uuid.UUID,
        title: str | None = None,
        body_md: str | None = None,
        dept: str | None = None,
        domain: str | None = None,
        type_: str | None = None,
        sensitivity: str | None = None,
        language: str | None = None,
        status_: str | None = None,
        tags: list[str] | None = None,
        access_group_ids: list[uuid.UUID] | None = None,
        next_review: datetime | None = None
    ) -> Article:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        # Authorization check
        if not PermissionService.can_edit_article(user, article):
            raise HTTPException(status_code=403, detail="Not authorized to edit this article")

        # Track changes for permission recalculation and version incrementing
        permissions_changed = False
        content_changed = False
        published_transition = False

        if sensitivity is not None and sensitivity != article.sensitivity:
            article.sensitivity = sensitivity
            permissions_changed = True

        if language is not None and language != article.language:
            article.language = language
            content_changed = True

        if access_group_ids is not None:
            new_groups = list(await self.user_repo.get_groups_by_ids(access_group_ids))
            # Compare access groups
            old_group_ids = {g.id for g in article.access_groups}
            new_group_ids = {g.id for g in new_groups}
            if old_group_ids != new_group_ids:
                article.access_groups = new_groups
                permissions_changed = True

        if title is not None and title != article.title:
            article.title = title
            content_changed = True

        if body_md is not None and body_md != article.body_md:
            article.body_md = body_md
            content_changed = True

        if dept is not None and dept != article.dept:
            article.dept = dept
            permissions_changed = True
            content_changed = True

        if domain is not None and domain != article.domain:
            article.domain = domain
            content_changed = True

        if type_ is not None and type_ != article.type:
            article.type = type_
            content_changed = True

        if status_ is not None and status_ != article.status:
            published_transition = status_ == "published" and article.status != "published"
            article.status = status_
            if status_ == "published":
                article.last_reviewed = datetime.utcnow()
            content_changed = True

        if next_review is not None:
            article.next_review = next_review

        # If content changed, bump the version and save version snapshot
        if content_changed:
            article.version += 1

        updated_article = await self.article_repo.update(article)

        if tags is not None:
            await self.article_repo.sync_tags(updated_article.id, tags)
            # Re-fetch tags
            updated_article = await self.article_repo.get_by_id(updated_article.id)

        if content_changed:
            tag_strings = [t.tag for t in updated_article.tags]
            snapshot = {
                "title": updated_article.title,
                "body_md": updated_article.body_md,
                "dept": updated_article.dept,
                "domain": updated_article.domain,
                "type": updated_article.type,
                "sensitivity": updated_article.sensitivity,
                "language": updated_article.language,
                "tags": tag_strings
            }
            version = ArticleVersion(
                article_id=updated_article.id,
                version=updated_article.version,
                snapshot=snapshot,
                edited_by=user.id
            )
            await self.article_repo.create_version(version)

        # Trigger events
        if permissions_changed:
            await event_bus.publish("PermissionChanged", {"article_id": str(updated_article.id)})
            await self._audit(user.id, "permission_change", updated_article.id)

        if content_changed:
            await self._audit(user.id, "update", updated_article.id)
            if updated_article.status == "published":
                if published_transition:
                    await self._audit(user.id, "publish", updated_article.id)
                logger.info("Published article re-indexing event queued", article_id=str(updated_article.id))
                await event_bus.publish("ArticleUpdated", {"article_id": str(updated_article.id)})

        return updated_article

    async def get_article(self, user: User, article_id: uuid.UUID) -> Article:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        return article

    async def soft_delete_article(self, user: User, article_id: uuid.UUID) -> None:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_delete_article(user, article):
            raise HTTPException(status_code=403, detail="Not authorized to delete this article")

        await self.article_repo.soft_delete(article_id)
        await self._audit(user.id, "delete", article_id)
        await event_bus.publish("ArticleDeleted", {"article_id": str(article_id)})

    async def get_history(self, user: User, article_id: uuid.UUID) -> Sequence[ArticleVersion]:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        return await self.article_repo.get_versions(article_id)

    async def get_version(self, user: User, article_id: uuid.UUID, version_num: int) -> ArticleVersion:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        version = await self.article_repo.get_version_by_number(article_id, version_num)
        if not version:
            raise HTTPException(status_code=404, detail="Version not found")
        return version

    async def restore_version(self, user: User, article_id: uuid.UUID, version_num: int) -> Article:
        """Restore a historical snapshot as a new active version.

        Historical snapshots are never overwritten. Restoring version 2 of a
        v4 article creates v5 with v2's content and keeps v1-v4 available for
        audit and comparison.
        """
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")
        if not PermissionService.can_edit_article(user, article):
            raise HTTPException(status_code=403, detail="Not authorized to restore this article version")

        snapshot_version = await self.article_repo.get_version_by_number(article_id, version_num)
        if not snapshot_version:
            raise HTTPException(status_code=404, detail="Version not found")
        if snapshot_version.version == article.version:
            raise HTTPException(status_code=409, detail="This version is already active")

        snapshot = snapshot_version.snapshot or {}
        for field in ("title", "body_md", "dept", "domain", "type", "sensitivity", "language"):
            if field in snapshot and snapshot[field] is not None:
                setattr(article, field, snapshot[field])

        article.version += 1
        article.lifecycle_status = "active"
        article.index_status = "pending"
        article.index_error = None
        restored_article = await self.article_repo.update(article)

        tags = snapshot.get("tags")
        if isinstance(tags, list):
            await self.article_repo.sync_tags(article.id, [str(tag) for tag in tags])
            restored_article = await self.article_repo.get_by_id(article.id)
        if not restored_article:
            raise HTTPException(status_code=500, detail="Failed to reload restored article")

        restored_snapshot = {
            "title": restored_article.title,
            "body_md": restored_article.body_md,
            "dept": restored_article.dept,
            "domain": restored_article.domain,
            "type": restored_article.type,
            "sensitivity": restored_article.sensitivity,
            "language": restored_article.language,
            "tags": [tag.tag for tag in restored_article.tags],
            "restored_from_version": version_num,
        }
        await self.article_repo.create_version(ArticleVersion(
            article_id=restored_article.id,
            version=restored_article.version,
            snapshot=restored_snapshot,
            edited_by=user.id,
        ))
        await self._audit(user.id, "restore_version", restored_article.id)

        if restored_article.status == "published":
            await event_bus.publish("ArticleUpdated", {"article_id": str(restored_article.id)})
        return restored_article
