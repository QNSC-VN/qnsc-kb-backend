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
    imports=["src.workers.tasks"]
)
