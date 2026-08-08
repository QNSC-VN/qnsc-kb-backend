import uuid
from datetime import datetime
from typing import Sequence
from sqlalchemy import case, select, delete, and_, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.governance import PendingDraft, Gap, AuditLog
from src.models.article import Article
from src.models.interaction import Vote
from src.models.ops import SearchLog, ApiRequestMetric
from src.models.ai import AiUsageLog

class GovernanceRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Pending Drafts
    async def create_draft(self, draft: PendingDraft) -> PendingDraft:
        self.db.add(draft)
        await self.db.commit()
        await self.db.refresh(draft)
        return draft

    async def get_draft(self, draft_id: uuid.UUID) -> PendingDraft | None:
        result = await self.db.execute(
            select(PendingDraft).where(PendingDraft.id == draft_id)
        )
        return result.scalar_one_or_none()

    async def list_drafts(self, status: str | None = None, company_domain: str | None = None, dept: str | None = None, depts: Sequence[str] | None = None) -> Sequence[PendingDraft]:
        stmt = select(PendingDraft)
        if status:
            stmt = stmt.where(PendingDraft.status == status)
        if company_domain:
            stmt = stmt.where(PendingDraft.company_domain == company_domain)
        if depts:
            stmt = stmt.where(PendingDraft.dept.in_(list(depts)))
        elif dept:
            stmt = stmt.where(PendingDraft.dept == dept)
        result = await self.db.execute(stmt.order_by(PendingDraft.created_at.desc()))
        return result.scalars().all()

    async def update_draft(self, draft: PendingDraft) -> PendingDraft:
        self.db.add(draft)
        await self.db.commit()
        await self.db.refresh(draft)
        return draft

    # Gap Queue
    async def log_gap(self, query: str, company_domain: str, dept: str | None = None) -> Gap:
        # A query can legitimately be a gap in more than one tenant.
        result = await self.db.execute(
            select(Gap).where(Gap.query == query, Gap.company_domain == company_domain)
        )
        gap = result.scalar_one_or_none()
        if gap:
            gap.count += 1
            gap.updated_at = datetime.utcnow()
        else:
            gap = Gap(query=query, company_domain=company_domain, count=1, dept=dept, status="open")
            self.db.add(gap)
        await self.db.commit()
        await self.db.refresh(gap)
        return gap

    async def list_gaps(self, status: str | None = None, company_domain: str | None = None) -> Sequence[Gap]:
        stmt = select(Gap)
        if status:
            stmt = stmt.where(Gap.status == status)
        if company_domain:
            stmt = stmt.where(Gap.company_domain == company_domain)
        result = await self.db.execute(stmt.order_by(Gap.count.desc()))
        return result.scalars().all()

    async def get_gap(self, gap_id: uuid.UUID, company_domain: str | None = None) -> Gap | None:
        stmt = select(Gap).where(Gap.id == gap_id)
        if company_domain:
            stmt = stmt.where(Gap.company_domain == company_domain)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_gap(self, gap: Gap) -> Gap:
        self.db.add(gap)
        await self.db.commit()
        await self.db.refresh(gap)
        return gap

    # Audit Logs
    async def log_audit(self, audit: AuditLog) -> AuditLog:
        self.db.add(audit)
        await self.db.commit()
        await self.db.refresh(audit)
        return audit

    async def list_audits(self, limit: int = 100, offset: int = 0) -> Sequence[AuditLog]:
        result = await self.db.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
            .options(selectinload(AuditLog.user))
        )
        return result.scalars().all()

    # Health Dashboard Metrics
    async def get_health_metrics(self) -> dict:
        now = datetime.utcnow()
        
        # 1. Total active (published) articles
        total_stmt = select(func.count(Article.id)).where(Article.status == "published")
        total_res = await self.db.execute(total_stmt)
        total_articles = total_res.scalar_one() or 0

        # 2. Articles with owner
        owner_stmt = select(func.count(Article.id)).where(and_(Article.status == "published", Article.owner_id.isnot(None)))
        owner_res = await self.db.execute(owner_stmt)
        articles_with_owner = owner_res.scalar_one() or 0

        # 3. Overdue for review
        overdue_stmt = select(func.count(Article.id)).where(and_(Article.status == "published", Article.next_review < now))
        overdue_res = await self.db.execute(overdue_stmt)
        overdue_articles = overdue_res.scalar_one() or 0

        # 4. Search Gaps Count (total open gaps)
        gaps_stmt = select(func.count(Gap.id)).where(Gap.status == "open")
        gaps_res = await self.db.execute(gaps_stmt)
        open_gaps = gaps_res.scalar_one() or 0

        # 5. Upvote/Downvote ratio (helpful rate)
        upvotes_stmt = select(func.count(Vote.id)).where(Vote.value == 1)
        upvotes_res = await self.db.execute(upvotes_stmt)
        upvotes = upvotes_res.scalar_one() or 0

        total_votes_stmt = select(func.count(Vote.id))
        total_votes_res = await self.db.execute(total_votes_stmt)
        total_votes = total_votes_res.scalar_one() or 0
        helpful_rate = (upvotes / total_votes * 100.0) if total_votes > 0 else 100.0

        search_total_res = await self.db.execute(select(func.count(SearchLog.id)))
        search_total = search_total_res.scalar_one() or 0
        search_miss_res = await self.db.execute(select(func.count(SearchLog.id)).where(SearchLog.result_count == 0))
        search_misses = search_miss_res.scalar_one() or 0

        ai_total_res = await self.db.execute(select(func.count(AiUsageLog.id)))
        ai_total = ai_total_res.scalar_one() or 0
        ai_cache_res = await self.db.execute(
            select(func.count(AiUsageLog.id)).where(AiUsageLog.prompt_version == "cached")
        )
        ai_cache_hits = ai_cache_res.scalar_one() or 0

        request_metrics = await self.db.execute(select(
            func.count(ApiRequestMetric.id),
            func.coalesce(func.sum(case((ApiRequestMetric.status_code >= 500, 1), else_=0)), 0),
            func.percentile_cont(0.95).within_group(ApiRequestMetric.duration_ms),
        ))
        request_count, error_requests, p95_latency = request_metrics.one()
        ai_usage_result = await self.db.execute(
            select(func.coalesce(func.sum(AiUsageLog.tokens_used), 0), func.coalesce(func.avg(AiUsageLog.latency_ms), 0))
        )
        ai_tokens_total, ai_latency_avg = ai_usage_result.one()

        percent_with_owner = (articles_with_owner / total_articles * 100.0) if total_articles > 0 else 0.0
        percent_overdue = (overdue_articles / total_articles * 100.0) if total_articles > 0 else 0.0

        return {
            "total_articles": total_articles,
            "percent_with_owner": percent_with_owner,
            "percent_overdue": percent_overdue,
            "open_gaps": open_gaps,
            "helpful_rate": helpful_rate,
            "search_miss_rate": (search_misses / search_total * 100.0) if search_total else 0.0,
            "ai_cache_hit_rate": (ai_cache_hits / ai_total * 100.0) if ai_total else 0.0,
            "api_request_count": int(request_count or 0),
            "api_error_rate": (int(error_requests or 0) / int(request_count) * 100.0) if request_count else 0.0,
            "api_p95_latency_ms": float(p95_latency or 0.0),
            "ai_requests": ai_total,
            "ai_tokens_total": int(ai_tokens_total or 0),
            "ai_average_latency_ms": float(ai_latency_avg or 0),
        }
