import asyncio
import uuid
import structlog
from datetime import datetime
from celery.signals import worker_ready, task_prerun, task_failure
from src.workers.celery_app import celery_app
from src.api.deps import SessionLocal
from src.repositories.article import ArticleRepository
from src.repositories.chunk import ChunkRepository
from src.repositories.user import UserRepository
from src.repositories.governance import GovernanceRepository
from src.domain.permissions import PermissionService
from src.domain.search_service import get_text_embedding
from src.core.config import settings
from src.models.chunk import ParentChunk, ArticleChunk
from src.models.governance import PendingDraft

logger = structlog.get_logger()

@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    logger.info("Celery worker ready", worker=str(sender))

@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, **kwargs):
    logger.info("Celery task started", task_name=getattr(task, "name", None), task_id=task_id)

@task_failure.connect
def task_failure_handler(task_id=None, exception=None, sender=None, **kwargs):
    logger.error(
        "Celery task failed",
        task_name=getattr(sender, "name", None),
        task_id=task_id,
        error=str(exception),
    )

def sync_run(coro):
    """Helper to run async coroutines synchronously in Celery worker thread"""
    # Celery executes tasks in worker threads/processes without an event loop.
    # get_event_loop() raises on Python 3.11 in that context, which previously
    # caused embedding tasks to exit before creating any chunks.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Cannot run a Celery async task inside an active event loop")

@celery_app.task(name="handle_domain_event_task")
def handle_domain_event_task(event_type: str, payload: dict):
    logger.info("Celery event worker received event", event_type=event_type, payload=payload)
    
    if event_type in ["ArticlePublished", "ArticleUpdated"]:
        article_id = payload.get("article_id")
        if article_id:
            generate_embeddings_task.delay(article_id)
            
    elif event_type == "PermissionChanged":
        article_id = payload.get("article_id")
        if article_id:
            recompute_permissions_task.delay(article_id)
            
    elif event_type == "ArticleDeleted":
        article_id = payload.get("article_id")
        if article_id:
            delete_article_chunks_task.delay(article_id)

@celery_app.task(name="generate_embeddings_task")
def generate_embeddings_task(article_id_str: str):
    article_id = uuid.UUID(article_id_str)
    logger.info("Generating embeddings for article", article_id=article_id)
    
    async def process():
        async with SessionLocal() as db:
            article_repo = ArticleRepository(db)
            chunk_repo = ChunkRepository(db)
            
            article = await article_repo.get_by_id(article_id)
            if not article or article.status != "published":
                logger.warn("Article not found or not published, skipping embeddings", article_id=article_id)
                return
                
            # Clear old chunks first
            await chunk_repo.delete_by_article_id(article_id)
            
            # Simple header-based parent/child chunking
            # Split article by double newlines or markdown headers
            sections = article.body_md.split("\n## ")
            
            for section_idx, section in enumerate(sections):
                lines = section.strip().split("\n")
                first_line = lines[0].strip() if lines else ""
                if first_line.startswith("#"):
                    section_ref = first_line.lstrip("#").strip()[:255] or f"Section {section_idx + 1}"
                    section_text = "\n".join(lines[1:])
                else:
                    section_ref = f"Section {section_idx + 1}"
                    section_text = section.strip()
                
                # Save parent chunk
                parent = ParentChunk(
                    article_id=article_id,
                    text=section_text,
                    section_ref=section_ref
                )
                parent = await chunk_repo.create_parent_chunk(parent)
                
                # Split parent chunk text into smaller child chunks (~300-500 chars)
                # E.g., split by sentences or paragraphs
                paragraphs = section_text.split("\n\n")
                child_idx = 0
                child_chunks_to_create = []
                
                for paragraph in paragraphs:
                    p_clean = paragraph.strip()
                    if not p_clean:
                        continue
                        
                    # Calculate child chunk embedding
                    embedding = await get_text_embedding(p_clean)
                    if embedding is None:
                        # Seeding a dummy mock embedding vector if API is disabled/missing
                        embedding = [0.0] * settings.EMBEDDING_DIMENSION
                        
                    # Calculate access bitmask
                    access_bitmap = PermissionService.calculate_article_bitmask(article)
                    
                    child = ArticleChunk(
                        article_id=article_id,
                        parent_chunk_id=parent.id,
                        chunk_text=p_clean,
                        embedding=embedding,
                        embedding_model=settings.EMBEDDING_MODEL,
                        embedding_version="v1.0",
                        access_group_bitmap=access_bitmap,
                        department_id=article.dept,
                        sensitivity=article.sensitivity,
                        visibility=article.sensitivity,
                        chunk_index=child_idx
                    )
                    child_chunks_to_create.append(child)
                    child_idx += 1
                    
                if child_chunks_to_create:
                    await chunk_repo.create_child_chunks(child_chunks_to_create)
                    
            logger.info("Successfully completed chunking and embedding generation", article_id=article_id)

    try:
        sync_run(process())
    except Exception:
        logger.exception("Embedding generation task failed", article_id=article_id)
        raise

@celery_app.task(name="recompute_permissions_task")
def recompute_permissions_task(article_id_str: str):
    article_id = uuid.UUID(article_id_str)
    logger.info("Recomputing permission bitmask snapshot on chunks", article_id=article_id)
    
    async def process():
        async with SessionLocal() as db:
            article_repo = ArticleRepository(db)
            chunk_repo = ChunkRepository(db)
            
            article = await article_repo.get_by_id(article_id)
            if not article:
                logger.warn("Article not found, skipping permission recomputation", article_id=article_id)
                return
                
            bitmap = PermissionService.calculate_article_bitmask(article)
            await chunk_repo.update_permissions(
                article_id=article_id,
                bitmap=bitmap,
                sensitivity=article.sensitivity,
                visibility=article.sensitivity,
                dept=article.dept
            )
            logger.info("Permission bitmap updated successfully", article_id=article_id, bitmap=bitmap)

    sync_run(process())

@celery_app.task(name="delete_article_chunks_task")
def delete_article_chunks_task(article_id_str: str):
    article_id = uuid.UUID(article_id_str)
    logger.info("Deleting chunks for removed article", article_id=article_id)
    
    async def process():
        async with SessionLocal() as db:
            chunk_repo = ChunkRepository(db)
            await chunk_repo.delete_by_article_id(article_id)
            logger.info("Article chunks deleted successfully", article_id=article_id)

    sync_run(process())

@celery_app.task(name="sync_connector_job_task")
def sync_connector_job_task(connector_id_str: str):
    connector_id = uuid.UUID(connector_id_str)
    logger.info("Starting connector sync job", connector_id=connector_id)
    
    async def process():
        async with SessionLocal() as db:
            gov_repo = GovernanceRepository(db)
            # Create a mock pending draft representing a newly ingested file
            draft = PendingDraft(
                title=f"Mock SOP document ingested {datetime.utcnow().strftime('%Y-%m-%d')}",
                source_ref=f"google_drive://folders/qnsc-kb/{uuid.uuid4()}.pdf",
                source_hash=str(uuid.uuid4().hex),
                summary="This standard procedure defines company protocols for emergency server restarts and system patching cycles.",
                status="pending"
            )
            await gov_repo.create_draft(draft)
            logger.info("Ingestion completed: created pending draft", draft_id=draft.id)
            
    sync_run(process())
