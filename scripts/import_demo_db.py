"""Persist JSON produced by import_demo.py into the configured database."""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from src.api.deps import SessionLocal
from src.models.article import Article
from src.models.user import User


async def run(seed_path: Path, email: str) -> None:
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    async with SessionLocal() as db:
        owner = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not owner:
            raise RuntimeError(f"No user found for --email {email}; create an admin first.")
        records = payload.get("articles", [])
        by_external: dict[str, Article] = {}
        for record in records:
            existing = (await db.execute(select(Article).where(Article.external_id == record["external_id"]))).scalar_one_or_none()
            article = existing or Article(external_id=record["external_id"])
            article.title = record.get("title") or record["external_id"]
            article.body_md = record.get("body_text") or record.get("summary") or ""
            article.dept = record.get("dept") or "Company Spine"
            article.domain = record.get("domain") or "General"
            article.type = record.get("type") or "REFERENCE"
            article.sensitivity = record.get("sensitivity") or "internal"
            article.language = "vi"
            article.owner_id = owner.id
            article.company_domain = owner.company_domain
            article.status = "published" if record.get("status") == "published" else "draft"
            article.version = int(record.get("version") or 1)
            article.last_reviewed = _date(record.get("last_reviewed"))
            article.next_review = _date(record.get("next_review"))
            article.needs_update = False
            article.related_article_ids = record.get("related") or []
            if not existing:
                db.add(article)
            by_external[article.external_id] = article
        await db.commit()
        print(f"Imported or refreshed {len(by_external)} demo articles for {owner.email}.")


def _date(value: str | None):
    return datetime.fromisoformat(value) if value else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("seed", type=Path)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.seed, args.email))
