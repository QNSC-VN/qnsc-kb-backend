"""`article_lock` must release even when the body fails inside an aborted transaction.

Session-level advisory locks outlive the transaction that took them and are released
only by an explicit unlock or by the connection closing. If the body raises, the
transaction is aborted and Postgres rejects the unlock too — so a naive `finally` leaks
the lock onto a pooled connection that stays open for the life of the process. That is
not recoverable in-band: the pool's reset-on-return is a ROLLBACK, which does not touch
advisory locks, so every later attempt on that article blocks forever.

It happened: one failed index left an article stuck in `index_status='processing'` with a
lock held by an idle backend, and reindexing timed out until the backend was terminated.
"""
from __future__ import annotations

import pytest

from src.lib.locking import article_lock

ARTICLE_ID = "69808cf8-f04d-412b-8250-ae29653511db"


class FakeSession:
    """Rejects statements once the transaction is aborted, as Postgres does."""

    def __init__(self, fail_body: bool) -> None:
        self.fail_body = fail_body
        self.aborted = False
        self.statements: list[str] = []
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored")
        self.statements.append(sql)
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False


def _unlocks(session: FakeSession) -> int:
    return sum(1 for sql in session.statements if "pg_advisory_unlock" in sql)


@pytest.mark.asyncio
async def test_releases_lock_on_success() -> None:
    session = FakeSession(fail_body=False)
    async with article_lock(session, ARTICLE_ID):
        pass
    assert _unlocks(session) == 1
    assert session.rollbacks == 0, "a successful body must not lose its uncommitted work"


@pytest.mark.asyncio
async def test_releases_lock_when_body_aborted_the_transaction() -> None:
    session = FakeSession(fail_body=True)
    with pytest.raises(ValueError):
        async with article_lock(session, ARTICLE_ID):
            session.aborted = True  # what a failed statement leaves behind
            raise ValueError("indexing failed")
    assert session.rollbacks == 1, "the aborted transaction must be discarded"
    assert _unlocks(session) == 1, "the advisory lock must still be released"
