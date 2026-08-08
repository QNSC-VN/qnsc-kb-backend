import uuid
import asyncio

import pytest
from fastapi import HTTPException

from src.domain.governance import GovernanceService
from src.models.governance import PendingDraft
from src.models.rbac import Permission, Role, RolePermission
from src.models.user import User


def reviewer(email: str, company: str = "acme.test") -> User:
    user = User(id=uuid.uuid4(), email=email, name=email, company_domain=company, role="Reviewer", active=True)
    permission = Permission(key="article.review", name="Review")
    role = Role(name="Reviewer", company_domain=company)
    role.permissions.append(RolePermission(permission=permission, scope="company"))
    user.roles.append(role)
    return user


class FakeGovernanceRepository:
    def __init__(self, draft: PendingDraft):
        self.draft = draft
        class Db:
            def add(self, _item):
                pass
            async def commit(self):
                pass
        self.db = Db()
        self.audits = []

    async def get_draft(self, draft_id):
        return self.draft if draft_id == self.draft.id else None

    async def update_draft(self, draft):
        self.draft = draft
        return draft

    async def log_audit(self, audit):
        self.audits.append(audit)
        return audit


class FakeArticleRepository:
    pass


def test_draft_cannot_be_approved_before_assignment():
    author = User(id=uuid.uuid4(), email="author@acme.test", name="Author", company_domain="acme.test", role="Staff")
    draft = PendingDraft(id=uuid.uuid4(), title="Procedure", company_domain="acme.test", source_ref="upload://procedure.pdf", source_hash="a" * 64, created_by=author.id, status="pending")
    service = GovernanceService(FakeGovernanceRepository(draft), FakeArticleRepository())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(service.approve_draft(reviewer("reviewer@acme.test"), draft.id))

    assert exc.value.status_code == 409
    assert "Assign an approver" in str(exc.value.detail)


def test_only_assigned_reviewer_can_reject(monkeypatch):
    author = User(id=uuid.uuid4(), email="author@acme.test", name="Author", company_domain="acme.test", role="Staff")
    assigned = reviewer("assigned@acme.test")
    other = reviewer("other@acme.test")
    draft = PendingDraft(id=uuid.uuid4(), title="Procedure", company_domain="acme.test", source_ref="upload://procedure.pdf", source_hash="a" * 64, created_by=author.id, assigned_approver_id=assigned.id, status="pending")
    service = GovernanceService(FakeGovernanceRepository(draft), FakeArticleRepository())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(service.reject_draft(other, draft.id, "Not ready"))
    assert exc.value.status_code == 403

    rejected = asyncio.run(service.reject_draft(assigned, draft.id, "Missing required steps"))
    assert rejected.status == "rejected"
    assert rejected.reviewed_by == assigned.id


def test_manual_revision_cannot_be_published_as_a_new_article():
    author = User(id=uuid.uuid4(), email="author@acme.test", name="Author", company_domain="acme.test", role="Staff")
    assigned = reviewer("assigned@acme.test")
    original_article_id = uuid.uuid4()
    draft = PendingDraft(
        id=uuid.uuid4(), title="Procedure", company_domain="acme.test", source_ref="manual-update://procedure",
        source_hash="a" * 64, created_by=author.id, assigned_approver_id=assigned.id, status="pending",
        requires_update_confirmation=True,
        content_metadata={
            "submission_kind": "manual_update",
            "suggested_update_article_id": str(original_article_id),
            "sensitivity": "public",
        },
    )
    service = GovernanceService(FakeGovernanceRepository(draft), FakeArticleRepository())

    with pytest.raises(HTTPException, match="must update its original article"):
        asyncio.run(service.approve_draft(assigned, draft.id, treat_as_new=True))
