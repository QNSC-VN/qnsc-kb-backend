from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

logger = structlog.get_logger()
from src.models.article import Article
from src.models.user import User
from src.repositories.article import ArticleRepository

GLOSSARY_ITEMS = [
    {"term": "SOP", "definition": "Standard Operating Procedure - detailed step-by-step instructions to achieve consistency."},
    {"term": "RCA", "definition": "Root Cause Analysis - method of problem-solving used for identifying the root causes of faults or problems."},
    {"term": "RAG", "definition": "Retrieval-Augmented Generation - optimizing the output of a large language model with authoritative external sources."},
    {"term": "IdP", "definition": "Identity Provider - service that creates, maintains, and manages identity information."},
    {"term": "SCIM", "definition": "System for Cross-domain Identity Management - open standard for automating user identity information exchange."},
    {"term": "SLO", "definition": "Service Level Objective - target level of service performance."},
    {"term": "SLI", "definition": "Service Level Indicator - quantitative measure of the service level provided."}
]

class MetaService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_all_tags(self, user: User) -> list[str]:
        articles = await ArticleRepository(self.db).list_articles(user, status="published")
        tags = sorted({tag.tag for article in articles for tag in article.tags})
        logger.info("Meta tags loaded", tag_count=len(tags))
        return tags

    async def get_glossary(self) -> list[dict[str, str]]:
        logger.info("Meta glossary loaded", glossary_item_count=len(GLOSSARY_ITEMS))
        return GLOSSARY_ITEMS
