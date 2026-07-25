import uuid
from typing import Sequence
from fastapi import HTTPException
from src.models.user import User
from src.models.interaction import Comment, Vote, Bookmark
from src.models.article import Article
from src.repositories.interaction import InteractionRepository
from src.repositories.article import ArticleRepository
from src.domain.permissions import PermissionService

class InteractionsService:
    def __init__(self, interaction_repo: InteractionRepository, article_repo: ArticleRepository):
        self.interaction_repo = interaction_repo
        self.article_repo = article_repo

    async def add_comment(self, user: User, article_id: uuid.UUID, text: str) -> Comment:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        comment = Comment(
            article_id=article_id,
            user_id=user.id,
            text=text
        )
        return await self.interaction_repo.create_comment(comment)

    async def get_comments(self, user: User, article_id: uuid.UUID) -> Sequence[Comment]:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        return await self.interaction_repo.get_comments(article_id)

    async def delete_comment(self, user: User, comment_id: uuid.UUID) -> bool:
        deleted = await self.interaction_repo.delete_comment(comment_id, user.id)
        if not deleted:
            raise HTTPException(status_code=403, detail="Comment not found or unauthorized to delete")
        return True

    # Upvotes/Downvotes
    async def cast_vote(self, user: User, article_id: uuid.UUID, value: int) -> dict:
        if value not in [1, -1, 0]:
            raise HTTPException(status_code=400, detail="Invalid vote value")

        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        vote = Vote(
            article_id=article_id,
            user_id=user.id,
            value=value
        )
        await self.interaction_repo.cast_vote(vote)
        
        # Return new summaries
        return await self.interaction_repo.get_votes_summary(article_id)

    async def get_user_vote(self, user: User, article_id: uuid.UUID) -> int:
        return await self.interaction_repo.get_user_vote(article_id, user.id)

    async def get_votes_summary(self, user: User, article_id: uuid.UUID) -> dict[str, int]:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")
        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        return await self.interaction_repo.get_votes_summary(article_id)

    # Bookmarks
    async def add_bookmark(self, user: User, article_id: uuid.UUID) -> bool:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        if not PermissionService.can_view_article(user, article):
            raise HTTPException(status_code=403, detail="Access denied")

        bookmark = Bookmark(user_id=user.id, article_id=article_id)
        await self.interaction_repo.add_bookmark(bookmark)
        return True

    async def remove_bookmark(self, user: User, article_id: uuid.UUID) -> bool:
        return await self.interaction_repo.remove_bookmark(user.id, article_id)

    async def list_bookmarks(self, user: User) -> Sequence[Article]:
        return await self.interaction_repo.get_bookmarks(user.id)

    async def is_bookmarked(self, user: User, article_id: uuid.UUID) -> bool:
        return await self.interaction_repo.is_bookmarked(user.id, article_id)
