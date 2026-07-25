import uuid
from datetime import datetime, timedelta
from fastapi import HTTPException
from src.models.user import User
from src.models.article import Article
from src.repositories.article import ArticleRepository
from src.domain.permissions import PermissionService
from src.domain.events import event_bus

class ReviewService:
    def __init__(self, article_repo: ArticleRepository):
        self.article_repo = article_repo

    async def schedule_review(
        self,
        user: User,
        article_id: uuid.UUID,
        next_review_date: datetime
    ) -> Article:
        article = await self.article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail="Article not found")

        # Must be Owner, Dept Owner, or Admin
        if not PermissionService.can_edit_article(user, article):
            raise HTTPException(status_code=403, detail="Not authorized to set review schedule")

        article.next_review = next_review_date
        updated_article = await self.article_repo.update(article)
        
        await event_bus.publish("ReviewScheduled", {"article_id": str(article_id), "next_review": next_review_date.isoformat()})
        return updated_article

    async def verify_review_deadlines(self) -> list[str]:
        """
        Scans articles list for overdue reviews and fires ReviewExpired events.
        """
        # For simplicity, we can load active articles through repository
        # In a real setup, this is run by a Celery Beat task
        # We can implement a simple scan and return list of flagged IDs
        now = datetime.utcnow()
        # Seed an admin user representation to fetch list
        system_user = User(role="Admin")
        articles = await self.article_repo.list_articles(system_user)
        
        overdue_ids = []
        for article in articles:
            if article.next_review and article.next_review < now:
                overdue_ids.append(str(article.id))
                await event_bus.publish("ReviewExpired", {"article_id": str(article.id)})
                
        return overdue_ids
