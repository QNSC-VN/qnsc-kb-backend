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
    await db.execute(text("SELECT pg_advisory_lock(hashtext(:lock_key))"), {"lock_key": lock_key})
    try:
        yield
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(hashtext(:lock_key))"), {"lock_key": lock_key})
