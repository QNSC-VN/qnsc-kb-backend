import asyncio
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from src.api.deps import get_current_user, get_db
from src.domain.ai_service import AIService, normalize_answer_markdown
from src.domain.search_service import SearchService
from src.domain.permissions import PermissionService
from src.models import User
from src.models.article import Article
from src.models.chunk import ArticleChunk, ParentChunk
from src.repositories.ai import AIRepository
from src.repositories.chunk import ChunkRepository
from src.repositories.governance import GovernanceRepository
from src.core.rate_limit import ai_rate_limiter
from src.repositories.feature_flags import FeatureFlagRepository

router = APIRouter()
logger = structlog.get_logger()

class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    conversation_id: uuid.UUID | None = None

class ConversationCreate(BaseModel):
    title: str | None = None

class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=255)

class FeedbackRequest(BaseModel):
    ai_usage_log_id: uuid.UUID
    rating: Literal[-1, 1]
    comment: str | None = None

def get_ai_service(db: AsyncSession) -> AIService:
    chunk_repo = ChunkRepository(db)
    gov_repo = GovernanceRepository(db)
    return AIService(AIRepository(db), SearchService(chunk_repo, gov_repo, FeatureFlagRepository(db)), gov_repo)


async def _hydrate_citations(db: AsyncSession, user: User, citations: list[dict]) -> list[dict]:
    """Refresh historical citations with their authorized parent passage."""
    chunk_ids = []
    for citation in citations:
        try:
            if citation.get("chunk_id"):
                chunk_ids.append(uuid.UUID(str(citation["chunk_id"])))
        except (ValueError, TypeError):
            continue
    if not chunk_ids:
        return citations

    conditions = [
        ArticleChunk.id.in_(chunk_ids),
        Article.status == "published",
        Article.lifecycle_status == "active",
        ArticleChunk.access_group_bitmap.op("&")(PermissionService.calculate_user_bitmask(user)) != 0,
    ]
    if user.role != "Admin":
        conditions.append(Article.company_domain == user.company_domain)
    result = await db.execute(
        select(ArticleChunk)
        .join(Article, Article.id == ArticleChunk.article_id)
        .options(selectinload(ArticleChunk.parent_chunk).selectinload(ParentChunk.child_chunks))
        .where(*conditions)
    )
    chunks_by_id = {str(chunk.id): chunk for chunk in result.scalars().all()}
    hydrated = []
    for citation in citations:
        chunk = chunks_by_id.get(str(citation.get("chunk_id")))
        if chunk and chunk.parent_chunk:
            hydrated.append({
                **citation,
                "excerpt": chunk.parent_chunk.text[:1800],
                "highlight_text": chunk.chunk_text[:500],
                "highlight_texts": [child.chunk_text for child in sorted(chunk.parent_chunk.child_chunks, key=lambda item: item.chunk_index)],
                "page_number": chunk.page_number or chunk.parent_chunk.page_number,
            })
        else:
            hydrated.append(citation)
    return hydrated

@router.get("/conversations")
async def list_conversations(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    conversations = await AIRepository(db).list_conversations(current_user.id)
    return [{"id": str(item.id), "title": item.title, "updated_at": item.updated_at} for item in conversations]

@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(req: ConversationCreate | None = None, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    conversation = await AIRepository(db).create_conversation(current_user.id, (req.title if req else None) or "New conversation")
    return {"id": str(conversation.id), "title": conversation.title, "updated_at": conversation.updated_at}

@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    repo = AIRepository(db)
    if not await repo.get_conversation(conversation_id, current_user.id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await repo.list_messages(conversation_id, current_user.id)
    response = []
    for message in messages:
        citations = await _hydrate_citations(db, current_user, json.loads(message.citations or "[]"))
        response.append({
            "id": str(message.id),
            "role": message.role,
            "content": normalize_answer_markdown(message.content),
            "citations": citations,
            "usage_log_id": str(message.usage_log_id) if message.usage_log_id else None,
            "created_at": message.created_at,
        })
    return response

@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    repo = AIRepository(db)
    conversation = await repo.get_conversation(conversation_id, current_user.id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await repo.delete_conversation(conversation)

@router.patch("/conversations/{conversation_id}")
async def rename_conversation(conversation_id: uuid.UUID, req: ConversationRename, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    repo = AIRepository(db)
    conversation = await repo.get_conversation(conversation_id, current_user.id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    renamed = await repo.rename_conversation(conversation, req.title)
    return {"id": str(renamed.id), "title": renamed.title, "updated_at": renamed.updated_at}

@router.post("/ask")
async def ask_question(req: AskRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    allowed, retry_after = ai_rate_limiter.allow(str(current_user.id))
    if not allowed:
        raise HTTPException(status_code=429, detail="AI request limit exceeded. Please try again shortly.", headers={"Retry-After": str(retry_after)})
    repo = AIRepository(db)
    conversation = await repo.get_conversation(req.conversation_id, current_user.id) if req.conversation_id else None
    if req.conversation_id and not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not conversation:
        conversation = await repo.create_conversation(current_user.id, req.question[:80])
    elif conversation.title == "New conversation":
        conversation.title = req.question[:80]
        await db.commit()

    await repo.add_message(conversation.id, "user", req.question)
    data = await get_ai_service(db).ask(current_user, req.question, conversation_id=conversation.id)
    await repo.add_message(
        conversation.id,
        "assistant",
        data["answer"],
        data.get("citations"),
        uuid.UUID(data["log_id"]) if data.get("log_id") else None,
    )
    data["conversation_id"] = str(conversation.id)
    return data


@router.post("/ask/stream")
async def ask_question_stream(req: AskRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """Stream a grounded answer and its citations using the DocNexus SSE contract."""
    allowed, retry_after = ai_rate_limiter.allow(str(current_user.id))
    if not allowed:
        raise HTTPException(status_code=429, detail="AI request limit exceeded. Please try again shortly.", headers={"Retry-After": str(retry_after)})

    repo = AIRepository(db)
    conversation = await repo.get_conversation(req.conversation_id, current_user.id) if req.conversation_id else None
    if req.conversation_id and not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not conversation:
        conversation = await repo.create_conversation(current_user.id, req.question[:80])
    elif conversation.title == "New conversation":
        conversation.title = req.question[:80]
        await db.commit()

    await repo.add_message(conversation.id, "user", req.question)
    conversation_id = str(conversation.id)
    async def event_stream():
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

        async def on_token(content: str) -> None:
            await queue.put({"type": "token", "content": content})

        async def on_replace(content: str) -> None:
            await queue.put({"type": "replace", "content": content})

        task = asyncio.create_task(
            get_ai_service(db).ask(
                current_user,
                req.question,
                conversation_id=conversation.id,
                on_token=on_token,
                on_replace=on_replace,
            )
        )
        streamed_content = False
        while not task.done() or not queue.empty():
            if queue.empty():
                await asyncio.sleep(0.01)
                continue
            event = await queue.get()
            if event.get("type") in {"token", "replace"}:
                streamed_content = True
            yield f"data: {json.dumps(event)}\n\n"

        try:
            data = await task
        except HTTPException as exc:
            logger.error("AI stream task failed", status_code=exc.status_code, detail=str(exc.detail))
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc.detail)})}\n\n"
            return
        except Exception as exc:
            logger.exception("AI stream task failed unexpectedly", error=str(exc))
            yield f"data: {json.dumps({'type': 'error', 'detail': 'AI generation failed. Please try again.'})}\n\n"
            return
        if not streamed_content:
            answer = data.get("answer", "")
            for start in range(0, len(answer), 48):
                yield f"data: {json.dumps({'type': 'token', 'content': answer[start:start + 48]})}\n\n"
                await asyncio.sleep(0)
        await repo.add_message(
            conversation.id,
            "assistant",
            data["answer"],
            data.get("citations"),
            uuid.UUID(data["log_id"]) if data.get("log_id") else None,
        )
        yield f"data: {json.dumps({'type': 'sources', 'sources': data.get('citations', [])})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id, 'log_id': data.get('log_id'), 'prompt_version': data.get('prompt_version'), 'retrieval_version': data.get('retrieval_version')})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    success = await get_ai_service(db).submit_feedback(
        user=current_user,
        log_id=req.ai_usage_log_id,
        rating=req.rating,
        comment=req.comment,
    )
    return {"success": success}
