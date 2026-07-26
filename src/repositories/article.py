import uuid
from typing import Sequence, Any
from sqlalchemy import select, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.article import Article, ArticleVersion, ArticleTag, article_access
from src.models.user import User

class ArticleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, article_id: uuid.UUID) -> Article | None:
        result = await self.db.execute(
            select(Article)
            .where(Article.id == article_id)
            .options(
                selectinload(Article.access_groups),
                selectinload(Article.tags),
                selectinload(Article.owner),
                selectinload(Article.sources),
            )
        )
        return result.scalar_one_or_none()

    async def list_articles(
        self,
        user: User,
        dept: str | None = None,
        type_: str | None = None,
        sensitivity: str | None = None,
        status: str | None = None,
        search_query: str | None = None
    ) -> Sequence[Article]:
        stmt = select(Article).options(
            selectinload(Article.access_groups),
            selectinload(Article.tags),
            selectinload(Article.owner),
            selectinload(Article.sources),
        )
        
        # Enforce RBAC at the query level (unless user is Admin)
        filters = [Article.lifecycle_status == "active"]
        if user.role == "CEO":
            filters.append(Article.company_domain == user.company_domain)
        elif user.role != "Admin":
            user_group_ids = [g.id for g in user.groups]
            
            # Non-admins see:
            # 1. Public articles
            # 2. Articles where they belong to one of the allowed access groups
            # 3. Department Owners can see all articles in their own department
            # 4. Article owner can see their own article
            permission_conditions = [
                Article.sensitivity == "public",
                Article.owner_id == user.id
            ]
            
            if user_group_ids:
                permission_conditions.append(
                    Article.access_groups.any(article_access.c.group_id.in_(user_group_ids))
                )
                
            if user.role == "Department Owner" and user.dept:
                permission_conditions.append(Article.dept == user.dept)
                
            filters.append(or_(*permission_conditions))
            
            # Non-admins cannot see raw "draft" articles unless they own them or are reviewers
            if user.role != "Reviewer":
                filters.append(or_(
                    Article.status == "published",
                    Article.owner_id == user.id
                ))
            else:
                filters.append(Article.status.in_(["published", "pending_review"]))
        
        # Apply standard filters
        if dept:
            filters.append(Article.dept == dept)
        if type_:
            filters.append(Article.type == type_)
        if sensitivity:
            filters.append(Article.sensitivity == sensitivity)
        if status:
            filters.append(Article.status == status)
        if search_query:
            filters.append(Article.title.ilike(f"%{search_query}%"))
            
        # Default exclude "deleted" articles (soft-delete status)
        filters.append(Article.status != "deleted")

        if filters:
            stmt = stmt.where(and_(*filters))

        result = await self.db.execute(stmt.order_by(Article.created_at.desc()))
        return result.scalars().all()

    async def list_related(self, user: User, article: Article, limit: int = 6) -> Sequence[Article]:
        """Return published, authorized articles related by taxonomy or tags."""
        stmt = select(Article).options(
            selectinload(Article.access_groups),
            selectinload(Article.tags),
            selectinload(Article.owner),
            selectinload(Article.sources),
        ).where(
            Article.id != article.id,
            Article.status == "published",
            Article.lifecycle_status == "active",
            or_(
                Article.dept == article.dept,
                Article.domain == article.domain,
                Article.tags.any(ArticleTag.tag.in_(
                    select(ArticleTag.tag).where(ArticleTag.article_id == article.id)
                )),
            ),
        )
        if user.role == "CEO":
            stmt = stmt.where(Article.company_domain == user.company_domain)
        elif user.role != "Admin":
            permission_conditions = [Article.sensitivity == "public", Article.owner_id == user.id]
            group_ids = [group.id for group in user.groups]
            if group_ids:
                permission_conditions.append(Article.access_groups.any(article_access.c.group_id.in_(group_ids)))
            if user.role == "Department Owner" and user.dept:
                permission_conditions.append(Article.dept == user.dept)
            stmt = stmt.where(or_(*permission_conditions))
        result = await self.db.execute(stmt.order_by(Article.created_at.desc()).limit(limit))
        return result.scalars().all()

    async def create(self, article: Article) -> Article:
        self.db.add(article)
        await self.db.commit()
        await self.db.refresh(article)
        return article

    async def update(self, article: Article) -> Article:
        self.db.add(article)
        await self.db.commit()
        await self.db.refresh(article)
        return article

    async def soft_delete(self, article_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            update(Article)
            .where(Article.id == article_id)
            .values(status="deleted")
        )
        await self.db.commit()
        return result.rowcount > 0

    async def create_version(self, version: ArticleVersion) -> ArticleVersion:
        self.db.add(version)
        await self.db.commit()
        await self.db.refresh(version)
        return version

    async def get_versions(self, article_id: uuid.UUID) -> Sequence[ArticleVersion]:
        result = await self.db.execute(
            select(ArticleVersion)
            .where(ArticleVersion.article_id == article_id)
            .order_by(ArticleVersion.version.desc())
            .options(selectinload(ArticleVersion.editor))
        )
        return result.scalars().all()

    async def get_version_by_number(self, article_id: uuid.UUID, version_num: int) -> ArticleVersion | None:
        result = await self.db.execute(
            select(ArticleVersion)
            .where(
                and_(
                    ArticleVersion.article_id == article_id,
                    ArticleVersion.version == version_num
                )
            )
            .options(selectinload(ArticleVersion.editor))
        )
        return result.scalar_one_or_none()

    async def sync_tags(self, article_id: uuid.UUID, tags: list[str]) -> None:
        # Delete existing tags
        from sqlalchemy import delete
        await self.db.execute(delete(ArticleTag).where(ArticleTag.article_id == article_id))
        
        # Add new tags
        for t in tags:
            self.db.add(ArticleTag(article_id=article_id, tag=t))
        await self.db.commit()
