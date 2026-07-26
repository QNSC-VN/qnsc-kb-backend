import re
from difflib import SequenceMatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.article import Article
from src.models.user import User

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()

def token_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

async def find_similar_documents(db: AsyncSession, user: User, content: str) -> list[dict]:
    normalized = normalize(content)
    stmt = select(Article).where(Article.status != "deleted", Article.lifecycle_status == "active").options(selectinload(Article.sources))
    if user.role != "Admin":
        stmt = stmt.where(Article.company_domain == user.company_domain)
    matches: list[dict] = []
    for article in (await db.execute(stmt)).scalars().all():
        candidate = normalize(article.body_md)
        sequence_score = SequenceMatcher(None, normalized, candidate).ratio() if normalized and candidate else 0.0
        score = max(sequence_score, token_similarity(normalized, candidate))
        if score >= 0.25:
            matches.append({"article_id": str(article.id), "title": article.title, "score": round(score, 4), "lifecycle_status": article.lifecycle_status})
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:5]

def classify_similarity(matches: list[dict]) -> str:
    score = matches[0]["score"] if matches else 0.0
    if score >= 0.999:
        return "exact"
    if score >= 0.85:
        return "very_high"
    if score >= 0.25:
        return "partial"
    return "none"
