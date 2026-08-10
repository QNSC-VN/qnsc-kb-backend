import asyncio
from datetime import datetime, timedelta, timezone

from src.core.config import settings
from src.workers import tasks


def test_orphan_sweep_deletes_only_old_unreferenced_objects(monkeypatch):
    now = datetime.now(timezone.utc)
    old_orphan = "s3://private-kb/sources/acme.test/aa/orphan.pdf"
    retained = "s3://private-kb/sources/acme.test/bb/retained.pdf"
    recent_orphan = "s3://private-kb/sources/acme.test/cc/recent.pdf"
    objects = [
        {"storage_key": old_orphan, "last_modified": now - timedelta(days=2)},
        {"storage_key": retained, "last_modified": now - timedelta(days=2)},
        {"storage_key": recent_orphan, "last_modified": now},
    ]
    deleted = []

    monkeypatch.setattr(settings, "SOURCE_STORAGE_BUCKET", "private-kb")
    monkeypatch.setattr(settings, "SOURCE_ORPHAN_GRACE_HOURS", 24)
    monkeypatch.setattr(
        "src.domain.source_storage.list_source_objects", lambda: objects
    )
    monkeypatch.setattr(
        "src.domain.source_storage.delete_source",
        lambda storage_key: deleted.append(storage_key),
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.responses = [[retained], [], []]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(self.responses.pop(0))

    async def fake_context(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tasks, "SessionLocal", lambda: Session())
    monkeypatch.setattr(tasks, "set_database_context", fake_context)

    deleted_count = asyncio.run(tasks._run_orphan_source_cleanup())

    assert deleted_count == 1
    assert deleted == [old_orphan]
