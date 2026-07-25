import structlog
import uuid
from typing import Callable, Awaitable

logger = structlog.get_logger()

class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}

    def subscribe(self, event_type: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)

    async def publish(self, event_type: str, payload: dict) -> None:
        logger.info("Publishing domain event", event_type=event_type, payload=payload)
        
        # 1. Local execution of subscribed async callbacks
        if event_type in self._listeners:
            for callback in self._listeners[event_type]:
                try:
                    await callback(payload)
                except Exception as e:
                    logger.error("Error in event listener callback", event_type=event_type, error=str(e))
        
        # Celery is intentionally disabled for the current development phase.
        # Execute the small indexing lifecycle in-process instead.
        article_id = payload.get("article_id")
        if article_id:
            from src.domain.indexing import (
                delete_article_chunks,
                index_article,
                recompute_article_permissions,
            )
            article_uuid = uuid.UUID(article_id)
            if event_type in ("ArticlePublished", "ArticleUpdated"):
                await index_article(article_uuid)
            elif event_type == "PermissionChanged":
                await recompute_article_permissions(article_uuid)
            elif event_type == "ArticleDeleted":
                await delete_article_chunks(article_uuid)

event_bus = EventBus()
