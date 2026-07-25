from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

logger = structlog.get_logger()
from src.models.article import Article, ArticleTag

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

    async def get_all_tags(self) -> list[str]:
        result = await self.db.execute(select(ArticleTag.tag).distinct())
        tags = list(result.scalars().all())
        logger.info("Meta tags loaded", tag_count=len(tags))
        return tags

    async def get_glossary(self) -> list[dict[str, str]]:
        logger.info("Meta glossary loaded", glossary_item_count=len(GLOSSARY_ITEMS))
        return GLOSSARY_ITEMS

    async def get_taxonomy(self) -> dict[str, list[str]]:
        """
        Dynamically aggregates all published article departments and domains.
        """
        result = await self.db.execute(
            select(Article.dept, Article.domain)
            .where(Article.status == "published")
            .distinct()
        )
        taxonomy = {}
        for row in result.all():
            dept, domain = row
            if dept not in taxonomy:
                taxonomy[dept] = []
            if domain not in taxonomy[dept]:
                taxonomy[dept].append(domain)
        logger.info(
            "Meta taxonomy loaded",
            published_department_count=len(taxonomy),
            published_domain_count=sum(len(domains) for domains in taxonomy.values()),
        )
        return taxonomy
