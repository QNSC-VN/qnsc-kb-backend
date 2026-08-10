import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.domain.governance import GovernanceService
from src.models.governance import AuditLog
from src.models.user import User
from src.repositories.audit import AuditRepository
from src.repositories.governance import GovernanceRepository


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []


class _DB:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


class _AuditDB:
    def __init__(self):
        self.added = []

    def add(self, entry):
        self.added.append(entry)

    async def commit(self):
        return None

    async def refresh(self, _entry):
        return None


def test_audit_query_is_filterable_and_global_scoped():
    db = _DB()
    repository = GovernanceRepository(db)
    actor = uuid.uuid4()
    asyncio.run(
        repository.list_audits(
            limit=20,
            user_id=actor,
            action="approve",
            start_time=datetime.utcnow() - timedelta(days=1),
            end_time=datetime.utcnow(),
        )
    )
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "audit_logs.user_id" in sql
    assert "audit_logs.action" in sql
    assert "audit_logs.created_at" in sql


def test_non_global_user_cannot_read_full_audit_log():
    class FakeRepository:
        db = object()

        async def list_audits(self, **_kwargs):
            raise AssertionError(
                "unauthorized actor must be rejected before querying audit rows"
            )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            GovernanceService(FakeRepository(), object()).list_audit_logs(
                User(role="Staff")
            )
        )
    assert error.value.status_code == 403


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_audit_repository_records_actor_action_target_time_and_outcome(outcome):
    db = _AuditDB()
    actor = uuid.uuid4()
    target_id = str(uuid.uuid4())

    asyncio.run(
        AuditRepository(db).record(
            actor, "approve", "draft", target_id, outcome=outcome
        )
    )

    entry = db.added[0]
    assert isinstance(entry, AuditLog)
    assert entry.user_id == actor
    assert entry.action == "approve"
    assert entry.target_type == "draft"
    assert entry.target_id == target_id
    assert entry.outcome == outcome
    assert AuditLog.__table__.c.created_at.server_default is not None
