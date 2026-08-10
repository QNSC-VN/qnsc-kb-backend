import uuid
import re

import structlog

from src.api.deps import SessionLocal, set_database_context
from src.core.config import settings
from src.domain.permissions import PermissionService
from src.domain.search_service import get_text_embeddings
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


def _child_chunk_metadata(parent_spec: dict, section_heading: str) -> tuple[str, str | None]:
    """Capture parent metadata while the parent spec is still in scope."""
    chunk_type = str(parent_spec.get("chunk_type") or "section")
    heading = str(parent_spec.get("heading") or section_heading or "")[:255] or None
    return chunk_type, heading


async def set_index_status(article_id: uuid.UUID, status: str, error: str | None = None) -> None:
    async with SessionLocal() as db:
        await set_database_context(db, None, True)
        article_repo = ArticleRepository(db)
        article = await article_repo.get_by_id(article_id)
        if article:
            article.index_status = status
            article.index_error = error
            await article_repo.update(article)


async def index_article(article_id: uuid.UUID) -> None:
    """Index an Article and persist a terminal failure state on exceptions."""
    await set_index_status(article_id, "processing")
    try:
        indexed = await _index_article(article_id)
        if not indexed:
            await set_index_status(
                article_id,
                "pending",
                "Article is not active and published; indexing was skipped",
            )
    except Exception as exc:
        logger.exception("Article indexing failed", article_id=str(article_id))
        try:
            await set_index_status(article_id, "failed", str(exc)[:2000])
        except Exception:
            # Preserve the original indexing exception if the status update
            # cannot be persisted, while leaving a diagnostic trail.
            logger.exception(
                "Unable to persist failed Article index status",
                article_id=str(article_id),
            )
        raise


async def _index_article(article_id: uuid.UUID) -> bool:
    """Create searchable chunks in-process while Celery is disabled."""
    async with SessionLocal() as db:
        await set_database_context(db, None, True)
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
                return False

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
                pending_children: list[tuple[str, int | None, uuid.UUID, str, str | None]] = []
                for parent_spec in create_parent_child_chunks(section_text, heading=section_heading):
                    parent_text = str(parent_spec["parent_text"])
                    parent_page_number = page_number or _match_source_page(parent_text, source_pages)
                    parent_chunk_type, parent_heading = _child_chunk_metadata(parent_spec, section_heading)
                    parent = await chunk_repo.create_parent_chunk(
                        ParentChunk(
                            article_id=article_id,
                            text=parent_text,
                            section_ref=section_ref,
                            chunk_type=parent_chunk_type,
                            heading=parent_heading,
                            page_number=parent_page_number,
                        )
                    )
                    for child_text in parent_spec["children"]:
                        clean_text = str(child_text).strip()
                        child_page_number = page_number or _match_source_page(clean_text, source_pages) or parent_page_number
                        pending_children.append((clean_text, child_page_number, parent.id, parent_chunk_type, parent_heading))

                embeddings = await get_text_embeddings([item[0] for item in pending_children])
                if embeddings is None or len(embeddings) != len(pending_children):
                    embedding_failures.extend({"section": section_ref, "chunk_index": index} for index in range(len(pending_children)))
                else:
                    for child_index, ((clean_text, child_page_number, parent_id, chunk_type, heading), embedding) in enumerate(zip(pending_children, embeddings)):
                        child_chunks.append(
                            ArticleChunk(
                                article_id=article_id,
                                parent_chunk_id=parent_id,
                                chunk_text=clean_text,
                                embedding=embedding,
                                embedding_model=settings.EMBEDDING_MODEL,
                                embedding_version=settings.EMBEDDING_VERSION,
                                chunk_type=chunk_type,
                                heading=heading,
                                chunking_version=settings.CHUNKING_VERSION,
                                access_group_bitmap=PermissionService.calculate_article_bitmask(article),
                                department_id=article.dept,
                                sensitivity=article.sensitivity,
                                visibility=article.visibility,
                                chunk_index=child_index,
                                page_number=child_page_number,
                            )
                        )

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
                raise RuntimeError(f"Embedding unavailable for {len(embedding_failures)} chunk(s)")
            article.index_status = "ready"
            article.index_error = None
            await db.commit()
            return True


async def recompute_article_permissions(article_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await set_database_context(db, None, True)
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
                visibility=article.visibility,
                dept=article.dept,
            )
            logger.info("Article permissions refreshed", article_id=str(article_id))


async def delete_article_chunks(article_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await set_database_context(db, None, True)
        await ChunkRepository(db).delete_by_article_id(article_id)
        logger.info("Article chunks deleted", article_id=str(article_id))
