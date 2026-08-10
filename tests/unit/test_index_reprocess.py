import asyncio
import uuid

import pytest

from src.api.routers.governance import _run_inline_index_reprocess
from src.domain.indexing import index_article


def test_inline_reprocess_runs_sync_task_off_the_api_event_loop(monkeypatch):
    calls = []

    async def fake_task(job_id: str):
        calls.append(job_id)

    monkeypatch.setattr("src.workers.tasks.run_reprocess_index_job", fake_task)
    job_id = uuid.uuid4()
    asyncio.run(_run_inline_index_reprocess(job_id))

    assert calls == [str(job_id)]


def test_indexing_failure_persists_failed_status(monkeypatch):
    statuses = []

    async def fake_set_index_status(article_id, status, error=None):
        statuses.append((article_id, status, error))

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class FailingArticleLock:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args):
            return False

    async def fake_database_context(*_args, **_kwargs):
        return None

    monkeypatch.setattr("src.domain.indexing.set_index_status", fake_set_index_status)
    monkeypatch.setattr("src.domain.indexing.SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        "src.domain.indexing.set_database_context", fake_database_context
    )
    monkeypatch.setattr(
        "src.domain.indexing.article_lock",
        lambda *_args: FailingArticleLock(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(index_article(uuid.uuid4()))

    assert statuses[0][1] == "processing"
    assert statuses[1][1] == "failed"
    assert statuses[1][2] == "database unavailable"


def test_indexing_skip_persists_non_stuck_pending_status(monkeypatch):
    article_id = uuid.uuid4()
    statuses = []

    async def fake_set_index_status(received_id, status, error=None):
        statuses.append((received_id, status, error))

    async def fake_index_article(_article_id):
        return False

    monkeypatch.setattr("src.domain.indexing.set_index_status", fake_set_index_status)
    monkeypatch.setattr("src.domain.indexing._index_article", fake_index_article)

    asyncio.run(index_article(article_id))

    assert statuses == [
        (article_id, "processing", None),
        (
            article_id,
            "pending",
            "Article is not active and published; indexing was skipped",
        ),
    ]
