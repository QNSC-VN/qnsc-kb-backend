import uuid

import structlog

from src.api.deps import SessionLocal
from src.core.config import settings
from src.domain.permissions import PermissionService
from src.domain.search_service import get_text_embedding
from src.models.chunk import ArticleChunk, ParentChunk
from src.repositories.article import ArticleRepository
from src.repositories.chunk import ChunkRepository

logger = structlog.get_logger()


async def index_article(article_id: uuid.UUID) -> None:
    """Create searchable chunks in-process while Celery is disabled."""
    async with SessionLocal() as db:
        article_repo = ArticleRepository(db)
        chunk_repo = ChunkRepository(db)
        article = await article_repo.get_by_id(article_id)

        if not article or article.status != "published":
            logger.warning(
                "Skipping article indexing",
                article_id=str(article_id),
                reason="article missing or not published",
                status=article.status if article else None,
            )
            return

        await chunk_repo.delete_by_article_id(article_id)
        sections = article.body_md.split("\n## ")
        chunk_count = 0

        for section_idx, section in enumerate(sections):
            lines = section.strip().split("\n")
            first_line = lines[0].strip() if lines else ""
            if first_line.startswith("#"):
                section_ref = first_line.lstrip("#").strip()[:255] or f"Section {section_idx + 1}"
                section_text = "\n".join(lines[1:])
            else:
                section_ref = f"Section {section_idx + 1}"
                section_text = section.strip()
            parent = await chunk_repo.create_parent_chunk(
                ParentChunk(article_id=article_id, text=section_text, section_ref=section_ref)
            )

            child_chunks = []
            for child_idx, paragraph in enumerate(section_text.split("\n\n")):
                clean_text = paragraph.strip()
                if not clean_text:
                    continue
                embedding = await get_text_embedding(clean_text)
                if embedding is None:
                    logger.warning(
                        "Article chunk embedding unavailable",
                        article_id=str(article_id),
                        chunk_index=child_idx,
                    )
                    continue
                child_chunks.append(
                    ArticleChunk(
                        article_id=article_id,
                        parent_chunk_id=parent.id,
                        chunk_text=clean_text,
                        embedding=embedding,
                        embedding_model=settings.EMBEDDING_MODEL,
                        embedding_version="v1.0",
                        access_group_bitmap=PermissionService.calculate_article_bitmask(article),
                        department_id=article.dept,
                        sensitivity=article.sensitivity,
                        visibility=article.sensitivity,
                        chunk_index=child_idx,
                    )
                )

            if child_chunks:
                await chunk_repo.create_child_chunks(child_chunks)
                chunk_count += len(child_chunks)

        logger.info("Article indexing completed", article_id=str(article_id), chunk_count=chunk_count)


async def recompute_article_permissions(article_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        article_repo = ArticleRepository(db)
        chunk_repo = ChunkRepository(db)
        article = await article_repo.get_by_id(article_id)
        if not article:
            logger.warning("Skipping permission refresh; article not found", article_id=str(article_id))
            return
        await chunk_repo.update_permissions(
            article_id=article_id,
            bitmap=PermissionService.calculate_article_bitmask(article),
            sensitivity=article.sensitivity,
            visibility=article.sensitivity,
            dept=article.dept,
        )
        logger.info("Article permissions refreshed", article_id=str(article_id))


async def delete_article_chunks(article_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await ChunkRepository(db).delete_by_article_id(article_id)
        logger.info("Article chunks deleted", article_id=str(article_id))
