import httpx
import structlog
import time
from typing import Any
from fastapi import HTTPException
from src.core.config import settings
from src.models.user import User
from src.models.governance import Gap
from src.models.ai import AiUsageLog
from src.repositories.chunk import ChunkRepository
from src.repositories.governance import GovernanceRepository
from src.domain.permissions import PermissionService
from src.rag.reranker import normalize_query, rerank_chunks
from src.models.ops import SearchLog
from src.repositories.feature_flags import FeatureFlagRepository

logger = structlog.get_logger()

import asyncio
from src.lib.embeddings import get_bge_embedding

async def get_text_embedding(text: str) -> list[float] | None:
    try:
        embedding = await asyncio.to_thread(get_bge_embedding, text)
        logger.info(
            "Search embedding generated",
            query_length=len(text),
            embedding_dimension=len(embedding) if embedding else 0,
            embedding_model=settings.EMBEDDING_MODEL,
        )
        return embedding
    except Exception as e:
        logger.error(
            "Error generating local BGE embedding; continuing with keyword search",
            error=str(e),
            embedding_model=settings.EMBEDDING_MODEL,
        )
        return None

class SearchService:
    def __init__(self, chunk_repo: ChunkRepository, gov_repo: GovernanceRepository, feature_flags: FeatureFlagRepository | None = None):
        self.chunk_repo = chunk_repo
        self.gov_repo = gov_repo
        self.feature_flags = feature_flags

    async def search(
        self,
        user: User,
        query: str,
        filters: dict | None = None,
        limit: int = 5
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        started = time.perf_counter()

        user_bitmask = PermissionService.calculate_user_bitmask(user)
        effective_filters = dict(filters or {})
        if user.role != "Admin":
            effective_filters["company_domain"] = user.company_domain
        logger.info(
            "Search started",
            query=query,
            limit=limit,
            filters=effective_filters,
            user_id=str(user.id),
            user_role=user.role,
            user_department=user.dept,
            access_group_count=len(user.groups),
            user_access_bitmask=user_bitmask,
        )
        
        # Question words such as "what is" / "là gì" are not useful search
        # terms. Normalize them before both vector and keyword retrieval so
        # the exact subject (for example, CTS) is not drowned out by generic
        # language. Keep the original query for diagnostics and gap logging.
        retrieval_query = normalize_query(query)
        logger.info(
            "Search query normalized",
            query=query,
            retrieval_query=retrieval_query,
        )

        # 1. Get embedding asynchronously
        embedding = await get_text_embedding(retrieval_query)
        
        # 2. Query hybrid search
        candidates = await self.chunk_repo.hybrid_search(
            query=retrieval_query,
            query_embedding=embedding,
            user_bitmask=user_bitmask,
            limit=max(30, limit * 6),
            filters=effective_filters
        )
        reranking_enabled = not self.feature_flags or await self.feature_flags.is_enabled("rag.reranker", user)
        chunks = rerank_chunks(retrieval_query, candidates, limit=limit) if reranking_enabled else candidates[:limit]
        logger.info(
            "Search repository completed",
            query=query,
            embedding_available=embedding is not None,
            candidate_count=len(candidates),
            reranking_enabled=reranking_enabled,
            result_count=len(chunks),
        )

        # 3. Log search query gap if no results found
        if not chunks:
            logger.info(
                "Search returned zero results, logging gap",
                query=query,
                reason="no published, permission-matching chunks matched vector or keyword search",
                user_access_bitmask=user_bitmask,
                filters=effective_filters,
            )
            await self.gov_repo.log_gap(query=query, dept=user.dept)

        # 4. Format search results
        formatted_results = []
        for idx, chunk in enumerate(chunks):
            parent = chunk.parent_chunk
            article = chunk.article
            
            # Simple score simulation for output ranking presentation
            score = 1.0 - (idx * 0.05) if idx < 20 else 0.1
            
            formatted_results.append({
                "chunk_id": str(chunk.id),
                "article_id": str(article.id),
                "title": article.title,
                "dept": article.dept,
                "domain": article.domain,
                "type": article.type,
                "sensitivity": article.sensitivity,
                "chunk_text": chunk.chunk_text,
                "parent_text": parent.text if parent else chunk.chunk_text,
                "child_texts": [child.chunk_text for child in parent.child_chunks] if parent else [chunk.chunk_text],
                "section_ref": parent.section_ref if parent else None,
                "page_number": chunk.page_number if chunk.page_number is not None else (parent.page_number if parent else None),
                "source_url": f"/api/v1/articles/{article.id}/source",
                "score": score
            })

        try:
            self.chunk_repo.db.add(SearchLog(
                user_id=user.id,
                query=query,
                result_count=len(formatted_results),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ))
            await self.chunk_repo.db.commit()
        except Exception as exc:
            logger.warning("Search log persistence failed", error=str(exc))
        return formatted_results
