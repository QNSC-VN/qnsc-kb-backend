import uuid
import re

import structlog

from src.api.deps import SessionLocal
from src.core.config import settings
from src.domain.permissions import PermissionService
from src.domain.search_service import get_text_embedding
from src.models.chunk import ArticleChunk, ParentChunk
from src.models.ops import DeadLetterJob
from src.models.article import DocumentSource
from sqlalchemy import select
from src.repositories.article import ArticleRepository
from src.repositories.chunk import ChunkRepository
from src.rag.chunker import create_parent_child_chunks
from src.lib.locking import article_lock

logger = structlog.get_logger()


def _normalized_tokens(value: str) -> list[str]:
    return re.findall(r"[\w'-]+", (value or "").lower())


def _match_source_page(text: str, source_pages: list[tuple[int, str]]) -> int | None:
    """Map structured article text back to the original extracted page."""
    query_tokens = _normalized_tokens(text)
    if not query_tokens or not source_pages:
        return None
    query_set = set(query_tokens)
    best_page: int | None = None
    best_score = 0.0
    for page_number, page_text in source_pages:
        page_tokens = _normalized_tokens(page_text)
        if not page_tokens:
            continue
        page_set = set(page_tokens)
        overlap = len(query_set & page_set) / max(min(len(query_set), 80), 1)
        # Longer exact runs are more reliable than common-token overlap.
        query_preview = " ".join(query_tokens[:24])
        page_normalized = " ".join(page_tokens)
        exact_bonus = 0.75 if len(query_preview) >= 24 and query_preview in page_normalized else 0.0
        score = overlap + exact_bonus
        if score > best_score:
            best_score = score
            best_page = page_number
    return best_page if best_score >= 0.12 else None


async def set_index_status(article_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionLocal() as db:
        article_repo = ArticleRepository(db)
        article = await article_repo.get_by_id(article_id)
        if article:
            article.index_status = status
            article.index_error = error
            await article_repo.update(article)


async def index_article(article_id: uuid.UUID) -> None:
    """Create searchable chunks in-process while Celery is disabled."""
    await set_index_status(article_id, "processing")
    async with SessionLocal() as db:
        async with article_lock(db, str(article_id)):
            article_repo = ArticleRepository(db)
            chunk_repo = ChunkRepository(db)
            article = await article_repo.get_by_id(article_id)

            if not article or article.status != "published" or article.lifecycle_status != "active":
                logger.warning(
                    "Skipping article indexing",
                    article_id=str(article_id),
                    reason="article missing or not published",
                    status=article.status if article else None,
                )
                return

            await chunk_repo.delete_by_article_id(article_id)
            source_result = await db.execute(
                select(DocumentSource)
                .where(DocumentSource.article_id == article_id)
                .order_by(DocumentSource.ingested_at.desc())
            )
            source = source_result.scalars().first()
            source_pages = []
            if source and source.page_texts:
                source_pages = [
                    (int(item.get("page_number", index)), str(item.get("text", "")))
                    for index, item in enumerate(source.page_texts, start=1)
                    if item.get("text")
                ]
            # Uploaded sources keep their original page text on DocumentSource
            # for audit/PDF review, while the article body is the approved,
            # losslessly restructured reading representation used for indexing.
            if article.body_md and article.body_md.strip():
                sections = [
                    (section.split("\n", 1)[0].strip() if section else "", section, None)
                    for section in article.body_md.split("\n## ")
                ]
            elif source and source.page_texts:
                sections = [
                    (f"Page {item.get('page_number', index)}", str(item.get("text", "")), item.get("page_number", index))
                    for index, item in enumerate(source.page_texts, start=1)
                    if item.get("text")
                ]
            else:
                sections = [(section.split("\n", 1)[0].strip() if section else "", section, None) for section in article.body_md.split("\n## ")]
            chunk_count = 0
            embedding_failures: list[dict[str, object]] = []

            for section_idx, (section_heading, section_body, page_number) in enumerate(sections):
                lines = section_body.strip().split("\n")
                first_line = lines[0].strip() if lines else ""
                if page_number is not None:
                    section_ref = section_heading[:255] or f"Page {page_number}"
                    section_text = section_body.strip()
                elif first_line.startswith("#"):
                    section_ref = first_line.lstrip("#").strip()[:255] or f"Section {section_idx + 1}"
                    section_text = "\n".join(lines[1:])
                else:
                    section_ref = f"Section {section_idx + 1}"
                    section_text = section_body.strip()
                child_chunks = []
                child_index = 0
                for parent_spec in create_parent_child_chunks(section_text):
                    parent_text = str(parent_spec["parent_text"])
                    parent_page_number = page_number or _match_source_page(parent_text, source_pages)
                    parent = await chunk_repo.create_parent_chunk(
                        ParentChunk(article_id=article_id, text=parent_text, section_ref=section_ref, page_number=parent_page_number)
                    )
                    for child_text in parent_spec["children"]:
                        clean_text = str(child_text).strip()
                        child_page_number = page_number or _match_source_page(clean_text, source_pages) or parent_page_number
                        embedding = await get_text_embedding(clean_text)
                        if embedding is None:
                            logger.warning("Article chunk embedding unavailable", article_id=str(article_id), chunk_index=child_index)
                            embedding_failures.append({"section": section_ref, "chunk_index": child_index})
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
                                chunk_index=child_index,
                                page_number=child_page_number,
                            )
                        )
                        child_index += 1

                if child_chunks:
                    await chunk_repo.create_child_chunks(child_chunks)
                    chunk_count += len(child_chunks)

            logger.info("Article indexing completed", article_id=str(article_id), chunk_count=chunk_count)
            if embedding_failures:
                db.add(DeadLetterJob(
                    source_queue="embedding",
                    payload={"article_id": str(article_id), "failures": embedding_failures},
                    error=f"Embedding unavailable for {len(embedding_failures)} chunk(s)",
                ))
                await db.commit()
                logger.warning("Embedding failures recorded in DLQ", article_id=str(article_id), failure_count=len(embedding_failures))
            article.index_status = "ready"
            article.index_error = None
            await db.commit()


async def recompute_article_permissions(article_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        async with article_lock(db, str(article_id)):
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
