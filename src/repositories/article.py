import uuid
from typing import Sequence, Any
from sqlalchemy import select, update, and_, or_, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.article import Article, ArticleVersion, ArticleTag, article_access
from src.models.user import User, Department
from src.domain.rbac import AuthorizationService
from src.domain.permissions import PermissionService

class ArticleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, article_id: uuid.UUID) -> Article | None:
        result = await self.db.execute(
            select(Article)
            .where(Article.id == article_id)
            .options(
                selectinload(Article.access_groups),
                selectinload(Article.departments),
                selectinload(Article.tags),
                selectinload(Article.owner),
                selectinload(Article.sources),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, article_id: uuid.UUID) -> Article | None:
        """Load an article row with a transaction lock for state transitions."""
        result = await self.db.execute(
            select(Article)
            .where(Article.id == article_id)
            .with_for_update()
            .options(
                selectinload(Article.access_groups),
                selectinload(Article.departments),
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
            selectinload(Article.departments),
            selectinload(Article.tags),
            selectinload(Article.owner),
            selectinload(Article.sources),
        )
        
        # Enforce RBAC at the query level (unless user is Admin)
        filters = [
            Article.lifecycle_status == "active",
            exists(select(Department.id).where(
                Department.company_domain == Article.company_domain,
                Department.name == Article.dept,
                Department.active.is_(True),
            )),
        ]
        can_read_global = AuthorizationService.has_permission(user, "article.read", requested_scope="global")
        can_read_department = AuthorizationService.has_permission(user, "article.read", requested_scope="department")
        full_company_access = AuthorizationService.has_full_company_article_access(user)
        if not can_read_global:
            filters.append(Article.company_domain == user.company_domain)
            if not full_company_access:
                member_departments = AuthorizationService.member_department_names(user)
                filters.append(or_(
                    Article.dept.in_(member_departments),
                    Article.departments.any(Department.name.in_(member_departments)),
                ))
            user_group_ids = [g.id for g in user.groups]
            
            # Non-admins see:
            # 1. Public articles
            # 2. Articles where they belong to one of the allowed access groups
            # 3. Department-scoped permissions can see articles in owned departments
            # 4. Article owner can see their own article
            permission_conditions = [
                Article.sensitivity == "public",
                Article.owner_id == user.id
            ]
            
            if user_group_ids and not full_company_access:
                permission_conditions.append(
                    Article.access_groups.any(article_access.c.group_id.in_(user_group_ids))
                )
                
            owned_departments = AuthorizationService.owned_department_names(user)
            if can_read_department and owned_departments:
                permission_conditions.append(or_(Article.dept.in_(owned_departments), Article.departments.any(Department.name.in_(owned_departments))))
                
            if not full_company_access:
                filters.append(or_(*permission_conditions))
            
            # Non-admins cannot see raw "draft" articles unless they own them or are reviewers
            if not AuthorizationService.has_permission(user, "article.review", requested_scope="company"):
                filters.append(or_(
                    Article.status == "published",
                    Article.owner_id == user.id
                ))
            else:
                filters.append(Article.status.in_(["published", "pending_review"]))
        
        # Apply standard filters
        if dept:
            filters.append(or_(Article.dept == dept, Article.departments.any(Department.name == dept)))
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
        # Query predicates are intentionally coarse for performance.  The
        # service-level decision is authoritative because group ACLs can be
        # stricter than ownership or department scope.
        visible_articles = []
        for article in result.scalars().all():
            if PermissionService.can_view_article(user, article):
                AuthorizationService.restrict_article_metadata(user, article)
                visible_articles.append(article)
        return visible_articles

    async def list_related(self, user: User, article: Article, limit: int = 6) -> Sequence[Article]:
        """Return published, authorized articles related by taxonomy or tags."""
        stmt = select(Article).options(
            selectinload(Article.access_groups),
            selectinload(Article.departments),
            selectinload(Article.tags),
            selectinload(Article.owner),
            selectinload(Article.sources),
        ).where(
            Article.id != article.id,
            Article.status == "published",
            Article.lifecycle_status == "active",
            exists(select(Department.id).where(
                Department.company_domain == Article.company_domain,
                Department.name == Article.dept,
                Department.active.is_(True),
            )),
            or_(
                or_(Article.dept == article.dept, Article.departments.any(Department.name == article.dept)),
                Article.domain == article.domain,
                Article.tags.any(ArticleTag.tag.in_(
                    select(ArticleTag.tag).where(ArticleTag.article_id == article.id)
                )),
            ),
        )
        can_read_global = AuthorizationService.has_permission(user, "article.read", requested_scope="global")
        can_read_department = AuthorizationService.has_permission(user, "article.read", requested_scope="department")
        full_company_access = AuthorizationService.has_full_company_article_access(user)
        if not can_read_global:
            stmt = stmt.where(Article.company_domain == user.company_domain)
            if not full_company_access:
                member_departments = AuthorizationService.member_department_names(user)
                stmt = stmt.where(or_(
                    Article.dept.in_(member_departments),
                    Article.departments.any(Department.name.in_(member_departments)),
                ))
            permission_conditions = [Article.sensitivity == "public", Article.owner_id == user.id]
            group_ids = [group.id for group in user.groups]
            if group_ids and not full_company_access:
                permission_conditions.append(Article.access_groups.any(article_access.c.group_id.in_(group_ids)))
            owned_departments = AuthorizationService.owned_department_names(user)
            if can_read_department and owned_departments:
                permission_conditions.append(or_(Article.dept.in_(owned_departments), Article.departments.any(Department.name.in_(owned_departments))))
            if not full_company_access:
                stmt = stmt.where(or_(*permission_conditions))
        result = await self.db.execute(stmt.order_by(Article.created_at.desc()).limit(limit * 3))
        visible_candidates = []
        for candidate in result.scalars().all():
            if PermissionService.can_view_article(user, candidate):
                AuthorizationService.restrict_article_metadata(user, candidate)
                visible_candidates.append(candidate)
        return visible_candidates[:limit]

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
