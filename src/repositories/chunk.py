import uuid
import re
import structlog
from typing import Sequence
from sqlalchemy import select, delete, update, and_, or_, text, func, exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.chunk import ParentChunk, ArticleChunk, ChunkMetadata
from src.models.article import Article
from src.models.user import Department
from src.core.config import settings

logger = structlog.get_logger()

class ChunkRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_parent_chunk(self, parent: ParentChunk) -> ParentChunk:
        self.db.add(parent)
        await self.db.commit()
        await self.db.refresh(parent)
        return parent

    async def create_child_chunks(self, chunks: list[ArticleChunk]) -> list[ArticleChunk]:
        for c in chunks:
            self.db.add(c)
        await self.db.commit()
        for c in chunks:
            await self.db.refresh(c)
        return chunks

    async def delete_by_article_id(self, article_id: uuid.UUID) -> None:
        await self.db.execute(delete(ParentChunk).where(ParentChunk.article_id == article_id))
        await self.db.execute(delete(ArticleChunk).where(ArticleChunk.article_id == article_id))
        await self.db.commit()

    async def get_by_article_id(self, article_id: uuid.UUID) -> Sequence[ArticleChunk]:
        result = await self.db.execute(
            select(ArticleChunk)
            .where(ArticleChunk.article_id == article_id)
            .order_by(ArticleChunk.chunk_index)
        )
        return result.scalars().all()

    async def get_parent_chunk(self, parent_id: uuid.UUID) -> ParentChunk | None:
        result = await self.db.execute(
            select(ParentChunk).where(ParentChunk.id == parent_id)
        )
        return result.scalar_one_or_none()

    async def authorized_chunk_ids(self, user: object, chunk_ids: list[uuid.UUID]) -> set[str]:
        """Return only citation chunks still visible to the current user."""
        if not chunk_ids:
            return set()
        from src.domain.permissions import PermissionService
        from src.domain.rbac import AuthorizationService

        conditions = [
            ArticleChunk.id.in_(chunk_ids),
            Article.status == "published",
            Article.lifecycle_status == "active",
            exists(select(Department.id).where(
                Department.company_domain == Article.company_domain,
                Department.name == Article.dept,
                Department.active.is_(True),
            )),
        ]
        if not AuthorizationService.has_full_company_article_access(user) and not AuthorizationService.has_narrow_article_access(user):
            conditions.append(ArticleChunk.access_group_bitmap.op("&")(PermissionService.calculate_user_bitmask(user)) != 0)
        if not AuthorizationService.has_permission(user, "article.read", requested_scope="global"):
            conditions.append(Article.company_domain == user.company_domain)
        result = await self.db.execute(
            select(ArticleChunk)
            .join(Article, Article.id == ArticleChunk.article_id)
            .options(selectinload(ArticleChunk.article))
            .where(*conditions)
        )
        return {
            str(chunk.id)
            for chunk in result.scalars().all()
            if PermissionService.can_view_article(user, chunk.article)
        }

    async def update_permissions(self, article_id: uuid.UUID, bitmap: int, sensitivity: str, visibility: str, dept: str) -> None:
        await self.db.execute(
            update(ArticleChunk)
            .where(ArticleChunk.article_id == article_id)
            .values(
                access_group_bitmap=bitmap,
                sensitivity=sensitivity,
                visibility=visibility,
                department_id=dept
            )
        )
        await self.db.commit()

    async def hybrid_search(
        self,
        query: str,
        query_embedding: list[float] | None,
        user_bitmask: int,
        limit: int = 5,
        filters: dict | None = None
    ) -> list[ArticleChunk]:
        """
        Executes a hybrid search:
        - If query_embedding is provided, calculates vector similarity.
        - Performs a full-text search on chunk_text using tsvector.
        - Merges the two lists using a reciprocal rank scoring mechanism.
        - Enforces access control natively: (access_group_bitmap & user_bitmask) != 0.
        """
        filters = filters or {}
        
        # Base filter: permissions bitwise AND
        # We also enforce that the article must be published (not draft or soft deleted)
        where_clauses = [
            # Join with articles to check status is published
            Article.status == "published"
            ,Article.lifecycle_status == "active"
        ]
        if not filters.get("bypass_access_groups"):
            where_clauses.insert(0, ArticleChunk.access_group_bitmap.op("&")(user_bitmask) != 0)

        if filters.get("company_domain"):
            where_clauses.append(Article.company_domain == filters["company_domain"])

        # Keep retrieval aligned with article browsing. Department-scoped
        # readers may search public content plus their explicitly owned
        # departments; own-scoped readers may search public content plus
        # their own articles. The final PermissionService check below remains
        # authoritative for group ACLs.
        scope_conditions = [Article.sensitivity == "public"]
        if filters.get("departments") is not None:
            department_names = list(filters["departments"])
            scope_conditions.append(or_(Article.dept.in_(department_names), Article.departments.any(Department.name.in_(department_names))))
        if filters.get("owner_id") is not None:
            scope_conditions.append(Article.owner_id == filters["owner_id"])
        if len(scope_conditions) > 1:
            where_clauses.append(or_(*scope_conditions))
        elif filters.get("departments") is not None or filters.get("owner_id") is not None:
            where_clauses.append(scope_conditions[0])

        # A deactivated department is no longer a valid content scope.
        where_clauses.append(exists(select(Department.id).where(
            Department.company_domain == Article.company_domain,
            Department.name == Article.dept,
            Department.active.is_(True),
        )))

        if filters.get("dept"):
            where_clauses.append(or_(ArticleChunk.department_id == filters["dept"], Article.departments.any(Department.name == filters["dept"])))
        if filters.get("sensitivity"):
            where_clauses.append(ArticleChunk.sensitivity == filters["sensitivity"])
        if filters.get("type"):
            where_clauses.append(Article.type == filters["type"])
        if filters.get("status"):
            where_clauses.append(Article.status == filters["status"])
        if filters.get("language"):
            where_clauses.append(Article.language == filters["language"])
        if filters.get("date_from"):
            where_clauses.append(Article.created_at >= filters["date_from"])
        if filters.get("date_to"):
            where_clauses.append(Article.created_at <= filters["date_to"])

        diagnostic_stmt = (
            select(func.count(ArticleChunk.id))
            .select_from(ArticleChunk)
            .join(Article, Article.id == ArticleChunk.article_id)
            .where(and_(*where_clauses))
        )
        diagnostic_res = await self.db.execute(diagnostic_stmt)
        eligible_count = diagnostic_res.scalar_one() or 0

        status_stmt = (
            select(Article.status, func.count(ArticleChunk.id))
            .select_from(ArticleChunk)
            .join(Article, Article.id == ArticleChunk.article_id)
            .group_by(Article.status)
        )
        status_res = await self.db.execute(status_stmt)
        chunk_counts_by_article_status = {
            status: count for status, count in status_res.all()
        }
        logger.info(
            "Search candidate scope",
            query=query,
            user_access_bitmask=user_bitmask,
            filters=filters,
            embedding_available=query_embedding is not None,
            eligible_published_chunks=eligible_count,
            chunk_counts_by_article_status=chunk_counts_by_article_status,
        )

        # 1. Vector Search
        vector_results = []
        if query_embedding is not None:
            # cosine_distance: <=> operator
            vector_stmt = (
                select(ArticleChunk)
                .join(Article, Article.id == ArticleChunk.article_id)
                .where(
                    and_(
                        *where_clauses,
                        ArticleChunk.embedding.cosine_distance(query_embedding) <= settings.VECTOR_DISTANCE_THRESHOLD,
                    )
                )
                .order_by(ArticleChunk.embedding.cosine_distance(query_embedding))
                .limit(max(30, limit * 6))
                .options(
                    selectinload(ArticleChunk.parent_chunk).selectinload(ParentChunk.child_chunks),
                    selectinload(ArticleChunk.article),
                )
            )
            vec_res = await self.db.execute(vector_stmt)
            vector_results = vec_res.scalars().all()
            logger.info(
                "Search vector candidates loaded",
                query=query,
                vector_result_count=len(vector_results),
            )

        # 2. Full-Text Search (keyword)
        # Fall back to ILIKE if postgres fails or for simplicity, but we can do proper FTS:
        keyword_terms = [term for term in re.findall(r"[\w'-]+", query.lower()) if len(term) > 1]
        keyword_conditions = [ArticleChunk.chunk_text.ilike(f"%{term}%") for term in keyword_terms]
        keyword_conditions.extend(Article.title.ilike(f"%{term}%") for term in keyword_terms)
        keyword_stmt = (
            select(ArticleChunk)
            .join(Article, Article.id == ArticleChunk.article_id)
            .where(
                and_(
                    *where_clauses,
                    or_(*keyword_conditions) if keyword_conditions else ArticleChunk.chunk_text.ilike(f"%{query}%")
                )
            )
            .order_by(
                func.ts_rank_cd(
                    func.to_tsvector("simple", func.concat_ws(" ", ArticleChunk.chunk_text, Article.title)),
                    func.plainto_tsquery("simple", query),
                ).desc()
            )
            .limit(30)
            .options(
                selectinload(ArticleChunk.parent_chunk).selectinload(ParentChunk.child_chunks),
                selectinload(ArticleChunk.article),
            )
        )
        key_res = await self.db.execute(keyword_stmt)
        keyword_results = key_res.scalars().all()
        logger.info(
            "Search keyword candidates loaded",
            query=query,
            keyword_result_count=len(keyword_results),
        )

        # 3. Merge results using Reciprocal Rank Fusion (RRF)
        rrf_scores = {}
        
        def add_rrf_scores(results_list):
            for rank, chunk in enumerate(results_list):
                # RRF formula: score = 1 / (60 + rank)
                score = 1.0 / (60.0 + rank)
                if chunk.id not in rrf_scores:
                    rrf_scores[chunk.id] = {"chunk": chunk, "score": 0.0}
                rrf_scores[chunk.id]["score"] += score

        add_rrf_scores(vector_results)
        add_rrf_scores(keyword_results)

        # Sort by score descending
        sorted_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)
        logger.info(
            "Search candidates merged",
            query=query,
            merged_result_count=len(sorted_results),
            returned_result_count=min(len(sorted_results), limit),
        )
        return [item["chunk"] for item in sorted_results[:limit]]
