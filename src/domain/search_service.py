import httpx
import structlog
from typing import Any
from fastapi import HTTPException
from src.core.config import settings
from src.models.user import User
from src.models.governance import Gap
from src.models.ai import AiUsageLog
from src.repositories.chunk import ChunkRepository
from src.repositories.governance import GovernanceRepository
from src.domain.permissions import PermissionService

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
    def __init__(self, chunk_repo: ChunkRepository, gov_repo: GovernanceRepository):
        self.chunk_repo = chunk_repo
        self.gov_repo = gov_repo

    async def search(
        self,
        user: User,
        query: str,
        filters: dict | None = None,
        limit: int = 5
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        user_bitmask = PermissionService.calculate_user_bitmask(user)
        logger.info(
            "Search started",
            query=query,
            limit=limit,
            filters=filters or {},
            user_id=str(user.id),
            user_role=user.role,
            user_department=user.dept,
            access_group_count=len(user.groups),
            user_access_bitmask=user_bitmask,
        )
        
        # 1. Get embedding asynchronously
        embedding = await get_text_embedding(query)
        
        # 2. Query hybrid search
        chunks = await self.chunk_repo.hybrid_search(
            query=query,
            query_embedding=embedding,
            user_bitmask=user_bitmask,
            limit=limit,
            filters=filters
        )
        logger.info(
            "Search repository completed",
            query=query,
            embedding_available=embedding is not None,
            result_count=len(chunks),
        )

        # 3. Log search query gap if no results found
        if not chunks:
            logger.info(
                "Search returned zero results, logging gap",
                query=query,
                reason="no published, permission-matching chunks matched vector or keyword search",
                user_access_bitmask=user_bitmask,
                filters=filters or {},
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
                "section_ref": parent.section_ref if parent else None,
                "score": score
            })

        return formatted_results
