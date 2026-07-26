import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from src.core.config import settings
from src.domain.source_extraction import extract_source_markdown, extract_source_pages, SourceExtractionError
from src.domain.source_storage import save_source
from src.models.governance import PendingDraft
from src.models.ops import Connector, ConnectorJob
from src.repositories.governance import GovernanceRepository
from src.domain.similarity import find_similar_documents, classify_similarity
from src.models.user import User

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

def _safe_folder(configured: str) -> Path:
    root = Path(settings.CONNECTOR_ROOT_PATH).resolve()
    candidate = Path(configured).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=422, detail=f"Local connector path must be inside {root}")
    return candidate

async def sync_local_folder(db: AsyncSession, connector: Connector) -> ConnectorJob:
    if connector.system != "local_folder":
        raise HTTPException(status_code=422, detail="Only local_folder is available until cloud authorization is configured")
    folder = _safe_folder(str((connector.config_json or {}).get("path", "")))
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="Connector folder does not exist")
    job = ConnectorJob(connector_id=connector.id, status="running", attempts=1)
    db.add(job)
    await db.commit()
    try:
        existing_hashes = set((await db.execute(select(PendingDraft.source_hash))).scalars().all())
        imported = 0
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            data = await asyncio.to_thread(path.read_bytes)
            source_hash = hashlib.sha256(data).hexdigest()
            if source_hash in existing_hashes:
                continue
            pages = await asyncio.to_thread(extract_source_pages, path.name, data)
            storage_key = await asyncio.to_thread(save_source, source_hash, path.name, data)
            text = await asyncio.to_thread(extract_source_markdown, path.name, data, pages)
            owner = (await db.execute(select(User).where(User.id == connector.created_by))).scalar_one_or_none()
            matches = await find_similar_documents(db, owner or User(role="Admin", company_domain=connector.company_domain), text)
            similarity_level = classify_similarity(matches)
            if similarity_level == "exact":
                continue
            db.add(PendingDraft(
                title=path.stem[:255], source_ref=f"local://{path.as_posix()}", source_hash=source_hash,
                summary=text, storage_key=storage_key, original_filename=path.name,
                mime_type="application/octet-stream", page_texts=pages, status="pending",
                similarity_level=similarity_level, similarity_matches=matches,
                requires_update_confirmation=similarity_level == "very_high",
                related_article_ids=[item["article_id"] for item in matches] if similarity_level == "partial" else None,
            ))
            existing_hashes.add(source_hash)
            imported += 1
        connector.last_sync = datetime.utcnow()
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        connector.status = "active"
        await db.commit()
        return job
    except (SourceExtractionError, OSError, ValueError) as exc:
        job.status = "failed"
        job.last_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=422, detail=f"Connector sync failed: {exc}") from exc
