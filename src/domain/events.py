try:
    import structlog
except ModuleNotFoundError:  # Optional logging dependency should not block pure domain tests.
    class _FallbackLogger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _FallbackStructlog:
        @staticmethod
        def get_logger():
            return _FallbackLogger()

    structlog = _FallbackStructlog()
import uuid
import asyncio
from typing import Callable, Awaitable

logger = structlog.get_logger()

class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self._background_tasks: set[asyncio.Task] = set()

    def subscribe(self, event_type: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)

    async def publish(self, event_type: str, payload: dict) -> None:
        logger.info("Publishing domain event", event_type=event_type, payload=payload)

        # Cached AI answers contain source text. Invalidate them before any
        # article lifecycle event completes so permission/content changes can
        # never serve stale grounded context.
        if event_type in {"ArticlePublished", "ArticleUpdated", "PermissionChanged", "ArticleDeleted"}:
            try:
                from sqlalchemy import delete
                from src.api.deps import SessionLocal
                from src.models.ai import AiCache
                async with SessionLocal() as db:
                    result = await db.execute(delete(AiCache))
                    await db.commit()
                    logger.info("AI cache invalidated after article event", event_type=event_type, invalidated_count=result.rowcount)
            except Exception as exc:
                logger.error("AI cache invalidation failed", event_type=event_type, error=str(exc))
                try:
                    from src.api.deps import SessionLocal
                    from src.models.ops import DeadLetterJob
                    async with SessionLocal() as db:
                        db.add(DeadLetterJob(source_queue=f"cache:{event_type}", payload=payload, error=str(exc)))
                        await db.commit()
                except Exception as dlq_error:
                    logger.error("Failed to persist cache invalidation dead-letter record", error=str(dlq_error))
        
        # 1. Local execution of subscribed async callbacks
        if event_type in self._listeners:
            for callback in self._listeners[event_type]:
                try:
                    await callback(payload)
                except Exception as e:
                    logger.error("Error in event listener callback", event_type=event_type, error=str(e))
                    try:
                        from src.api.deps import SessionLocal
                        from src.models.ops import DeadLetterJob
                        async with SessionLocal() as db:
                            db.add(DeadLetterJob(source_queue=event_type, payload=payload, error=str(e)))
                            await db.commit()
                    except Exception as dlq_error:
                        logger.error("Failed to persist event dead-letter record", event_type=event_type, error=str(dlq_error))
        
        # Celery is intentionally disabled for the current development phase.
        # Schedule the lifecycle in-process so publish/approval HTTP requests
        # return immediately, matching DocNexus's background ingestion UX.
        article_id = payload.get("article_id")
        if article_id:
            task = asyncio.create_task(self._run_article_lifecycle(event_type, uuid.UUID(article_id), payload))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _run_article_lifecycle(self, event_type: str, article_id: uuid.UUID, payload: dict) -> None:
        try:
            from src.domain.indexing import delete_article_chunks, index_article, recompute_article_permissions, set_index_status
            if event_type in ("ArticlePublished", "ArticleUpdated"):
                await index_article(article_id)
            elif event_type == "PermissionChanged":
                await recompute_article_permissions(article_id)
            elif event_type == "ArticleDeleted":
                await delete_article_chunks(article_id)
        except Exception as exc:
            logger.exception("Background article lifecycle failed", event_type=event_type, article_id=str(article_id), error=str(exc))
            if event_type in ("ArticlePublished", "ArticleUpdated"):
                try:
                    from src.domain.indexing import set_index_status
                    await set_index_status(article_id, "failed", str(exc))
                except Exception as status_error:
                    logger.error("Failed to update article index status", article_id=str(article_id), error=str(status_error))
            try:
                from src.api.deps import SessionLocal
                from src.models.ops import DeadLetterJob
                async with SessionLocal() as db:
                    db.add(DeadLetterJob(source_queue=event_type, payload=payload, error=str(exc)))
                    await db.commit()
            except Exception as dlq_error:
                logger.error("Failed to persist background lifecycle DLQ record", event_type=event_type, error=str(dlq_error))

event_bus = EventBus()
