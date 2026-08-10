from celery import Celery
from src.core.config import settings

celery_app = Celery(
    "qnsc_kb_workers",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    imports=["src.workers.tasks"],
    beat_schedule={
        "replay-domain-outbox": {
            "task": "replay_outbox_task",
            "schedule": 30.0,
        },
        "poll-cloud-connectors": {
            "task": "schedule_cloud_connector_syncs",
            "schedule": 600.0,
        },
        "prune-operational-metrics": {
            "task": "prune_operational_metrics",
            "schedule": 86400.0,
        },
        "cleanup-orphaned-source-objects": {
            "task": "cleanup_orphaned_source_objects",
            "schedule": 86400.0,
        },
    },
)
