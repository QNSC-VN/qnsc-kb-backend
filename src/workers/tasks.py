import uuid
import structlog
from datetime import datetime
from datetime import timedelta
from sqlalchemy import select, update
from celery.signals import worker_ready, worker_process_init, task_prerun, task_failure
from src.workers.celery_app import celery_app
from src.api.deps import SessionLocal, engine, set_database_context
from src.repositories.article import ArticleRepository
from src.repositories.chunk import ChunkRepository
from src.domain.permissions import PermissionService
from src.core.config import settings
from src.models.chunk import ParentChunk, ArticleChunk
from src.models.ops import ApiRequestMetric, OutboxEvent
from src.models.ai import AiCache
from src.workers.loop import reset_worker_loop, sync_run

logger = structlog.get_logger()


@worker_process_init.connect
def worker_process_init_handler(**kwargs):
    """Drop the parent process's asyncpg pool after Celery forks."""
    reset_worker_loop()
    engine.sync_engine.dispose(close=False)

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

    outbox_id = payload.get("_outbox_id")
    if outbox_id:
        async def mark_dispatched():
            async with SessionLocal() as db:
                await set_database_context(db, None, True)
                event = await db.get(OutboxEvent, uuid.UUID(outbox_id))
                if event:
                    event.status = "dispatched"
                    event.last_error = None
                    await db.commit()
        sync_run(mark_dispatched())


@celery_app.task(name="replay_outbox_task")
def replay_outbox_task():
    async def replay():
        async with SessionLocal() as db:
            await set_database_context(db, None, True)
            result = await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.status.in_(["pending", "failed", "processing"]), OutboxEvent.next_attempt_at <= datetime.utcnow())
                .order_by(OutboxEvent.created_at)
                .limit(100)
            )
            events = result.scalars().all()
            for event in events:
                event.status = "processing"
                event.attempts += 1
                event.next_attempt_at = datetime.utcnow() + timedelta(minutes=min(30, 2 ** min(event.attempts, 5)))
                await db.commit()
                payload = dict(event.payload)
                payload["_outbox_id"] = str(event.id)
                handle_domain_event_task.delay(event.event_type, payload)
    sync_run(replay())


@celery_app.task(name="prune_operational_metrics")
def prune_operational_metrics() -> None:
    """Bound telemetry growth and remove physically expired answer caches."""
    async def prune() -> None:
        async with SessionLocal() as db:
            await set_database_context(db, None, True)
            cutoff = datetime.utcnow() - timedelta(days=settings.METRICS_RETENTION_DAYS)
            await db.execute(
                ApiRequestMetric.__table__.delete().where(ApiRequestMetric.created_at < cutoff)
            )
            # Cache answers can contain authorized document passages. Their
            # six-hour expiry must remove storage as well as disable reads.
            await db.execute(AiCache.__table__.delete().where(AiCache.expires_at < datetime.utcnow()))
            await db.commit()
    sync_run(prune())

@celery_app.task(name="generate_embeddings_task")
def generate_embeddings_task(article_id_str: str):
    article_id = uuid.UUID(article_id_str)
    logger.info("Generating embeddings for article", article_id=article_id)
    
    async def process():
        from src.domain.indexing import index_article
        await index_article(article_id)

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
            await set_database_context(db, None, True)
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
            await set_database_context(db, None, True)
            chunk_repo = ChunkRepository(db)
            await chunk_repo.delete_by_article_id(article_id)
            logger.info("Article chunks deleted successfully", article_id=article_id)

    sync_run(process())

@celery_app.task(name="sync_cloud_connector_task", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sync_cloud_connector_task(connector_id_str: str, job_id_str: str):
    """Run an idempotent provider delta sync from a durable cursor."""
    async def process():
        from src.domain.cloud_sync import sync_cloud_connector
        from src.models.ops import Connector, ConnectorJob
        async with SessionLocal() as db:
            await set_database_context(db, None, True)
            connector = await db.get(Connector, uuid.UUID(connector_id_str))
            job = await db.get(ConnectorJob, uuid.UUID(job_id_str))
            if not connector or not job:
                return
            await sync_cloud_connector(db, connector, job)
    sync_run(process())


@celery_app.task(name="schedule_cloud_connector_syncs")
def schedule_cloud_connector_syncs():
    """Polling fallback: wake every active cloud connector periodically."""
    async def process():
        from datetime import timedelta
        from src.models.ops import Connector, ConnectorJob
        from src.models.connectors import SourceScope
        async with SessionLocal() as db:
            await set_database_context(db, None, True)
            connectors = (await db.execute(select(Connector).where(Connector.system.in_(["sharepoint", "google_drive"]), Connector.status.in_(["active", "error"])))).scalars().all()
            for connector in connectors:
                sync_mode = (connector.config_json or {}).get("sync_mode", "daily")
                if sync_mode == "manual":
                    continue
                interval = timedelta(days=1) if sync_mode == "daily" else timedelta(minutes=10)
                if connector.last_sync and connector.last_sync > datetime.utcnow() - interval:
                    continue
                selected = (await db.execute(select(SourceScope.id).where(SourceScope.connector_id == connector.id, SourceScope.selected.is_(True)).limit(1))).scalar_one_or_none()
                if not selected:
                    continue
                recent = (await db.execute(select(ConnectorJob.id).where(ConnectorJob.connector_id == connector.id, ConnectorJob.status.in_(["queued", "running"])).limit(1))).scalar_one_or_none()
                if recent:
                    continue
                job = ConnectorJob(connector_id=connector.id, status="queued", attempts=0)
                db.add(job)
                await db.commit()
                sync_cloud_connector_task.delay(str(connector.id), str(job.id))
    sync_run(process())


@celery_app.task(name="renew_webhook_subscriptions")
def renew_webhook_subscriptions():
    """Extend provider push subscriptions before they lapse.

    Microsoft Graph subscriptions on a drive are short-lived — create_webhook asks for one
    hour — and a lapsed subscription is not an error anyone sees: the provider simply
    stops calling, the connector keeps reporting `on_update`, and content silently goes
    stale until someone notices the KB is out of date. Nothing renewed them, so
    "real-time sync" lasted exactly one hour from the moment it was switched on.

    Runs every 10 minutes against a 20-minute horizon, so a subscription gets several
    attempts before it expires and one failed run is not enough to drop it.
    """
    async def process():
        from datetime import timedelta

        from src.domain.connector_adapters import ConnectorProviderError, adapter_for
        from src.models.connectors import WebhookSubscription
        from src.models.ops import Connector

        horizon = datetime.utcnow() + timedelta(minutes=20)
        async with SessionLocal() as db:
            await set_database_context(db, None, True)
            due = (await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.active.is_(True),
                    WebhookSubscription.expires_at.isnot(None),
                    WebhookSubscription.expires_at <= horizon,
                )
            )).scalars().all()
            for subscription in due:
                connector = await db.get(Connector, subscription.connector_id)
                if connector is None or connector.status not in ("active", "error"):
                    continue
                try:
                    renewed = await adapter_for(connector).renew_webhook(subscription.provider_subscription_id)
                except ConnectorProviderError as exc:
                    # The subscription is gone at the provider, or the token no longer
                    # grants it. Deactivate rather than retry forever: the polling loop in
                    # schedule_cloud_connector_syncs still covers this connector, so the
                    # cost is latency, not lost content.
                    logger.warning(
                        "Webhook subscription renewal failed",
                        connector_id=str(connector.id),
                        subscription_id=subscription.provider_subscription_id,
                        error=str(exc),
                    )
                    subscription.active = False
                    continue
                if renewed is None:
                    # Provider cannot extend in place (Google Drive channels). Retire it so
                    # the row does not claim a liveness it no longer has.
                    subscription.active = False
                    continue
                subscription.expires_at = renewed
            await db.commit()

    sync_run(process())
