import uuid
from typing import Sequence
from sqlalchemy import select, delete, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.interaction import Comment, Vote, Bookmark
from src.models.article import Article

class InteractionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Comments
    async def create_comment(self, comment: Comment) -> Comment:
        self.db.add(comment)
        await self.db.commit()
        await self.db.refresh(comment)
        # Load user info
        result = await self.db.execute(
            select(Comment).where(Comment.id == comment.id).options(selectinload(Comment.user))
        )
        return result.scalar_one()

    async def get_comments(self, article_id: uuid.UUID) -> Sequence[Comment]:
        result = await self.db.execute(
            select(Comment)
            .where(Comment.article_id == article_id)
            .order_by(Comment.created_at.asc())
            .options(selectinload(Comment.user))
        )
        return result.scalars().all()

    async def delete_comment(self, comment_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(Comment).where(and_(Comment.id == comment_id, Comment.user_id == user_id))
        )
        await self.db.commit()
        return result.rowcount > 0

    # Votes
    async def cast_vote(self, vote: Vote) -> Vote:
        # Delete any existing vote by this user on this article
        await self.db.execute(
            delete(Vote).where(and_(Vote.article_id == vote.article_id, Vote.user_id == vote.user_id))
        )
        self.db.add(vote)
        await self.db.commit()
        await self.db.refresh(vote)
        return vote

    async def get_votes_summary(self, article_id: uuid.UUID) -> dict[str, int]:
        result = await self.db.execute(
            select(Vote.value, func.count(Vote.id))
            .where(Vote.article_id == article_id)
            .group_by(Vote.value)
        )
        summary = {"upvotes": 0, "downvotes": 0}
        for row in result.all():
            val, cnt = row
            if val == 1:
                summary["upvotes"] = cnt
            elif val == -1:
                summary["downvotes"] = cnt
        return summary

    async def get_user_vote(self, article_id: uuid.UUID, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(Vote.value).where(and_(Vote.article_id == article_id, Vote.user_id == user_id))
        )
        return result.scalar_one_or_none() or 0

    # Bookmarks
    async def add_bookmark(self, bookmark: Bookmark) -> Bookmark:
        # Delete any duplicate first for idempotence
        await self.db.execute(
            delete(Bookmark).where(and_(Bookmark.article_id == bookmark.article_id, Bookmark.user_id == bookmark.user_id))
        )
        self.db.add(bookmark)
        await self.db.commit()
        return bookmark

    async def remove_bookmark(self, user_id: uuid.UUID, article_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(Bookmark).where(and_(Bookmark.user_id == user_id, Bookmark.article_id == article_id))
        )
        await self.db.commit()
        return result.rowcount > 0

    async def get_bookmarks(self, user_id: uuid.UUID) -> Sequence[Article]:
        result = await self.db.execute(
            select(Article)
            .join(Bookmark, Bookmark.article_id == Article.id)
            .where(and_(Bookmark.user_id == user_id, Article.status == "published"))
            .options(selectinload(Article.tags), selectinload(Article.owner), selectinload(Article.access_groups))
        )
        return result.scalars().all()

    async def is_bookmarked(self, user_id: uuid.UUID, article_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            select(Bookmark).where(and_(Bookmark.user_id == user_id, Bookmark.article_id == article_id))
        )
        return result.scalar_one_or_none() is not None
