import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Sequence
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from src.api.deps import get_db, get_current_user
from src.models import User
from src.repositories.article import ArticleRepository
from src.repositories.user import UserRepository
from src.domain.articles import ArticleService
from src.domain.source_extraction import SourceExtractionError, extract_source_markdown, extract_source_pages
from src.domain.source_storage import save_source, source_path
from src.models.article import DocumentSource
from src.models.governance import PendingDraft
from src.repositories.governance import GovernanceRepository
from src.repositories.feature_flags import FeatureFlagRepository
from src.core.config import settings
from src.repositories.audit import AuditRepository
from src.domain.similarity import find_similar_documents, classify_similarity
from src.domain.content_restructure import restructure_document
from src.domain.llm_client import complete, resolve_provider
from src.domain.permissions import PermissionService
import structlog

router = APIRouter()
logger = structlog.get_logger()

# Schema definitions
class ArticleCreate(BaseModel):
    title: str
    body_md: str
    dept: str
    domain: str
    type: str  # POLICY, SOP, DECISION, FAQ, RCA, HOWTO, PLAYBOOK, REFERENCE
    sensitivity: str  # public, internal, confidential, restricted
    language: str = "en"
    tags: list[str] = []
    access_group_ids: list[uuid.UUID] | None = None
    next_review: datetime | None = None

class ArticleUpdate(BaseModel):
    title: str | None = None
    body_md: str | None = None
    dept: str | None = None
    domain: str | None = None
    type: str | None = None
    sensitivity: str | None = None
    language: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    access_group_ids: list[uuid.UUID] | None = None
    next_review: datetime | None = None

class AutoTagRequest(BaseModel):
    article_ids: list[uuid.UUID]

class AutoTagResponse(BaseModel):
    results: list[dict]
    updated_count: int

class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    bitmask_position: int
    class Config:
        from_attributes = True

class TagResponse(BaseModel):
    tag: str
    class Config:
        from_attributes = True

class OwnerResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str

    class Config:
        from_attributes = True

class ArticleResponse(BaseModel):
    id: uuid.UUID
    title: str
    body_md: str
    dept: str
    domain: str
    company_domain: str
    type: str
    sensitivity: str
    language: str
    owner_id: uuid.UUID | None = None
    owner: OwnerResponse | None = None
    status: str
    lifecycle_status: str = "active"
    related_article_ids: list[str] | None = None
    version: int
    created_at: datetime
    next_review: datetime | None = None
    last_reviewed: datetime | None = None
    needs_update: bool = False
    index_status: str = "pending"
    index_error: str | None = None
    source_available: bool = False
    access_groups: list[GroupResponse] = []
    tags: list[TagResponse] = []

    class Config:
        from_attributes = True

class VersionResponse(BaseModel):
    id: uuid.UUID
    article_id: uuid.UUID
    version: int
    snapshot: dict
    created_at: datetime
    class Config:
        from_attributes = True

@router.post("/auto-tags", response_model=AutoTagResponse)
async def auto_tag_articles(
    request: AutoTagRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AutoTagResponse:
    """Generate and apply AI tags for a user-selected article batch."""
    unique_ids = list(dict.fromkeys(request.article_ids))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="Select at least one article")
    if len(unique_ids) > 20:
        raise HTTPException(status_code=400, detail="Select no more than 20 articles at a time")

    provider = resolve_provider()
    if not provider:
        raise HTTPException(status_code=503, detail="AI tagging is unavailable because no LLM provider is configured")

    article_repo = ArticleRepository(db)
    articles = []
    for article_id in unique_ids:
        article = await article_repo.get_by_id(article_id)
        if not article or article.status == "deleted":
            raise HTTPException(status_code=404, detail=f"Article {article_id} not found")
        if not PermissionService.can_edit_article(current_user, article):
            raise HTTPException(status_code=403, detail="You are not allowed to tag one or more selected articles")
        articles.append(article)

    documents = []
    for index, article in enumerate(articles, start=1):
        existing = [tag.tag for tag in article.tags]
        documents.append(
            f"ARTICLE {index}\nID: {article.id}\nTITLE: {article.title}\n"
            f"TYPE: {article.type}\nEXISTING TAGS: {', '.join(existing) or '(none)'}\n"
            f"CONTENT:\n{article.body_md[:5000]}"
        )
    prompt = (
        "Generate concise search tags for each article below. Return ONLY valid JSON in this exact shape: "
        '{"articles":[{"id":"article UUID","tags":["tag1","tag2"]}]}.'
        "Use 3 to 8 specific tags per article, in lowercase, with letters, numbers, spaces, or hyphens. "
        "Do not invent tags unrelated to the article. Keep existing tags when relevant and add useful missing tags.\n\n"
        + "\n\n".join(documents)
    )
    try:
        answer, _, _, _ = await complete(
            [
                {"role": "system", "content": "You are a precise knowledge-base taxonomy assistant. Output JSON only."},
                {"role": "user", "content": prompt},
            ],
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        cleaned = answer.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        payload = json.loads(cleaned)
        raw_results = payload.get("articles", []) if isinstance(payload, dict) else []
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("AI tag response was not valid JSON", error=str(exc), model=provider.model)
        raise HTTPException(status_code=502, detail="AI returned an invalid tag response")
    except Exception as exc:
        logger.error("AI tag generation failed", error=str(exc), model=provider.model)
        raise HTTPException(status_code=502, detail="AI tagging failed. Please try again.")

    suggestions_by_id: dict[str, list[str]] = {}
    for item in raw_results:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            continue
        cleaned_tags = []
        for tag in tags:
            value = re.sub(r"\s+", " ", str(tag).strip().lower())
            if value and len(value) <= 50 and re.fullmatch(r"[\w -]+", value, flags=re.UNICODE) and value not in cleaned_tags:
                cleaned_tags.append(value)
        suggestions_by_id[str(item["id"])] = cleaned_tags[:8]

    results = []
    for article in articles:
        current_tags = [tag.tag for tag in article.tags]
        suggestions = suggestions_by_id.get(str(article.id), [])
        merged_tags = list(dict.fromkeys([*current_tags, *suggestions]))[:20]
        if suggestions:
            await article_repo.sync_tags(article.id, merged_tags)
        results.append({"article_id": str(article.id), "title": article.title, "tags": merged_tags, "added_tags": [tag for tag in merged_tags if tag not in current_tags]})
    logger.info("AI tags applied", article_count=len(articles), updated_count=sum(bool(item["added_tags"]) for item in results), model=provider.model, user_id=str(current_user.id))
    return AutoTagResponse(results=results, updated_count=sum(bool(item["added_tags"]) for item in results))


@router.post("/upload-source", status_code=status.HTTP_201_CREATED)
async def upload_source(
    file: UploadFile = File(...),
    tags: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    data = await file.read()
    try:
        raw_tags = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        raw_tags = tags.split(",") if tags else []
    requested_tags = [tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()][:20]
    logger.info(
        "Source upload received",
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=len(data),
        user_id=str(current_user.id),
    )
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files must be 25 MB or smaller")
    filename = file.filename or "uploaded-source"
    try:
        extracted_pages = await asyncio.to_thread(extract_source_pages, filename, data)
    except SourceExtractionError as exc:
        logger.warning("Source extraction rejected", filename=filename, error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Source extraction failed", filename=filename, error=str(exc))
        raise HTTPException(status_code=422, detail=f"Could not process uploaded source: {exc}") from exc

    source_hash = hashlib.sha256(data).hexdigest()
    exact_stmt = select(DocumentSource).where(DocumentSource.source_hash == source_hash)
    if current_user.role != "Admin":
        exact_stmt = exact_stmt.join(DocumentSource.article).where(DocumentSource.article.has(company_domain=current_user.company_domain))
    exact_source = (await db.execute(exact_stmt)).scalars().first()
    if exact_source:
        raise HTTPException(status_code=409, detail={
            "code": "duplicate_document",
            "message": "This document already exists.",
            "article_id": str(exact_source.article_id) if exact_source.article_id else None,
        })
    extracted = await asyncio.to_thread(extract_source_markdown, filename, data, extracted_pages)
    restructuring_enabled = settings.RESTRUCTURE_ENABLED and await FeatureFlagRepository(db).is_enabled("ai.document_restructure", current_user)
    restructure_result = await restructure_document(filename, extracted, enabled=restructuring_enabled)
    matches = await find_similar_documents(db, current_user, extracted)
    similarity_level = classify_similarity(matches)
    if similarity_level == "exact":
        raise HTTPException(status_code=409, detail={"code": "duplicate_document", "message": "This document already exists.", "matches": matches})
    storage_key = await asyncio.to_thread(save_source, source_hash, filename, data)
    draft = PendingDraft(
        title=filename.rsplit(".", 1)[0][:255],
        source_ref=f"upload://{filename}",
        source_hash=source_hash,
        summary=extracted,
        restructured_body_md=restructure_result.body_md,
        restructure_status=restructure_result.status,
        restructure_model=restructure_result.model,
        restructure_error=restructure_result.error,
        storage_key=storage_key,
        original_filename=filename,
        mime_type=file.content_type or "application/octet-stream",
        page_texts=extracted_pages,
        created_by=current_user.id,
        status="pending",
        similarity_level=similarity_level,
        similarity_matches=matches,
        requires_update_confirmation=similarity_level == "very_high",
        related_article_ids=[item["article_id"] for item in matches] if similarity_level == "partial" else None,
        tags=requested_tags,
    )
    await GovernanceRepository(db).create_draft(draft)
    logger.info(
        "Source upload queued as pending draft",
        draft_id=str(draft.id),
        filename=filename,
        page_count=len(extracted_pages),
        extracted_characters=len(extracted),
        page_text_characters=sum(len(str(page["text"])) for page in extracted_pages),
        restructure_status=restructure_result.status,
        restructure_model=restructure_result.model,
    )
    return {
        "id": str(draft.id),
        "title": draft.title,
        "source_ref": draft.source_ref,
        "source_hash": draft.source_hash,
        "status": draft.status,
        "extracted_characters": len(extracted),
        "page_count": len(extracted_pages),
        "restructure_status": restructure_result.status,
        "restructure_model": restructure_result.model,
        "similarity_level": similarity_level,
        "similarity_matches": matches,
        "requires_update_confirmation": similarity_level == "very_high",
        "related_article_ids": [item["article_id"] for item in matches] if similarity_level == "partial" else [],
        "tags": requested_tags,
        "message": "Source extracted and queued for reviewer approval.",
    }


@router.post("/upload-sources", status_code=status.HTTP_201_CREATED)
async def upload_sources(
    files: list[UploadFile] = File(...),
    tags: list[str] | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Process a batch without letting one bad or duplicate file stop the batch."""
    if not files:
        raise HTTPException(status_code=422, detail="Select at least one file")
    results: list[dict[str, Any]] = []
    for index, file in enumerate(files):
        try:
            result = await upload_source(file=file, tags=tags[index] if tags and index < len(tags) else None, current_user=current_user, db=db)
            results.append({"filename": file.filename, "status": "queued", **result})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            results.append({
                "filename": file.filename,
                "status": "duplicate" if exc.status_code == 409 and detail.get("code") == "duplicate_document" else "failed",
                "status_code": exc.status_code,
                "detail": detail,
            })
    return {
        "results": results,
        "queued_count": sum(item["status"] == "queued" for item in results),
        "duplicate_count": sum(item["status"] == "duplicate" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
    }


@router.get("/{id}/source")
async def view_article_source(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Stream the authorized original source for inline PDF/page review."""
    article = await ArticleService(ArticleRepository(db), UserRepository(db)).get_article(current_user, id)
    result = await db.execute(
        select(DocumentSource)
        .where(DocumentSource.article_id == article.id)
        .order_by(DocumentSource.ingested_at.desc())
    )
    source = result.scalars().first()
    if not source or not source.storage_key:
        raise HTTPException(status_code=404, detail="Original source is not available for this article")
    path = source_path(source.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Original source file is missing")
    return FileResponse(
        path,
        media_type=source.mime_type or "application/pdf",
        filename=source.original_filename or article.title,
        content_disposition_type="inline",
    )

@router.post("/", response_model=ArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_article(
    article_in: ArticleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo, AuditRepository(db))
    restructuring_enabled = settings.RESTRUCTURE_ENABLED and await FeatureFlagRepository(db).is_enabled("ai.document_restructure", current_user)
    restructure_result = await restructure_document(article_in.title, article_in.body_md, enabled=restructuring_enabled)
    return await article_service.create_article(
        user=current_user,
        title=article_in.title,
        body_md=restructure_result.body_md,
        dept=article_in.dept,
        domain=article_in.domain,
        type_=article_in.type,
        sensitivity=article_in.sensitivity,
        language=article_in.language,
        tags=article_in.tags,
        access_group_ids=article_in.access_group_ids,
        next_review=article_in.next_review,
        original_body_md=article_in.body_md,
    )

@router.get("/", response_model=list[ArticleResponse])
async def list_articles(
    dept: str | None = Query(None),
    type_: str | None = Query(None, alias="type"),
    sensitivity: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    return await article_repo.list_articles(
        user=current_user,
        dept=dept,
        type_=type_,
        sensitivity=sensitivity,
        status=status,
        search_query=q
    )

@router.get("/{id}/related", response_model=list[ArticleResponse])
async def get_related_articles(
    id: uuid.UUID,
    limit: int = Query(6, ge=1, le=20),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article = await ArticleService(article_repo, user_repo).get_article(current_user, id)
    return await article_repo.list_related(current_user, article, limit)

@router.get("/{id}", response_model=ArticleResponse)
async def get_article(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo, AuditRepository(db))
    return await article_service.get_article(current_user, id)

@router.put("/{id}", response_model=ArticleResponse)
async def update_article(
    id: uuid.UUID,
    article_in: ArticleUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo, AuditRepository(db))
    return await article_service.update_article(
        user=current_user,
        article_id=id,
        title=article_in.title,
        body_md=article_in.body_md,
        dept=article_in.dept,
        domain=article_in.domain,
        type_=article_in.type,
        sensitivity=article_in.sensitivity,
        language=article_in.language,
        status_=article_in.status,
        tags=article_in.tags,
        access_group_ids=article_in.access_group_ids,
        next_review=article_in.next_review
    )

@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo, AuditRepository(db))
    await article_service.soft_delete_article(current_user, id)

@router.get("/{id}/versions", response_model=list[VersionResponse])
async def list_versions(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.get_history(current_user, id)

@router.get("/{id}/versions/{version_num}", response_model=VersionResponse)
async def get_version(
    id: uuid.UUID,
    version_num: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo)
    return await article_service.get_version(current_user, id, version_num)

@router.post("/{id}/versions/{version_num}/restore", response_model=ArticleResponse)
async def restore_version(
    id: uuid.UUID,
    version_num: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    article_repo = ArticleRepository(db)
    user_repo = UserRepository(db)
    article_service = ArticleService(article_repo, user_repo, AuditRepository(db))
    return await article_service.restore_version(current_user, id, version_num)
