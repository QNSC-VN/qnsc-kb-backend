"""Database-backed locks for article indexing and permission reconciliation."""
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def article_lock(db: AsyncSession, article_id: str) -> AsyncIterator[None]:
    """Serialize lifecycle work for one article across API processes.

    PostgreSQL session advisory locks stay held on the connection until the
    explicit unlock, including across the small commits used by the chunk
    repository. The connection is returned safely even when indexing fails.
    """
    lock_key = f"qnsc:article:{article_id}"
    unlock = text("SELECT pg_advisory_unlock(hashtext(:lock_key))")
    await db.execute(text("SELECT pg_advisory_lock(hashtext(:lock_key))"), {"lock_key": lock_key})
    try:
        yield
    finally:
        # The unlock has to survive a FAILED body, which is the case that matters. When
        # indexing raises, the transaction is already aborted, and Postgres rejects every
        # further statement in it — including this one. The unlock would then fail
        # silently inside `finally`, the connection would go back to the pool still
        # holding a SESSION-level advisory lock, and nothing would ever release it: the
        # pool's reset-on-return issues ROLLBACK, which does not touch advisory locks.
        # One failed index left an article permanently stuck in `processing`, because
        # every retry blocked on a lock held by an idle pooled connection.
        try:
            await db.execute(unlock, {"lock_key": lock_key})
        except Exception:
            # Discard the aborted transaction so the unlock can actually run. Any
            # uncommitted work is already lost — the body raised.
            await db.rollback()
            await db.execute(unlock, {"lock_key": lock_key})
