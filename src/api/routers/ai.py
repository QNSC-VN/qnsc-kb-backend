import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_db
from src.domain.ai_service import AIService
from src.domain.search_service import SearchService
from src.models import User
from src.repositories.ai import AIRepository
from src.repositories.chunk import ChunkRepository
from src.repositories.governance import GovernanceRepository

router = APIRouter()

class AskRequest(BaseModel):
    question: str
    conversation_id: uuid.UUID | None = None

class ConversationCreate(BaseModel):
    title: str | None = None

class FeedbackRequest(BaseModel):
    ai_usage_log_id: uuid.UUID
    rating: Literal[-1, 1]
    comment: str | None = None

def get_ai_service(db: AsyncSession) -> AIService:
    chunk_repo = ChunkRepository(db)
    gov_repo = GovernanceRepository(db)
    return AIService(AIRepository(db), SearchService(chunk_repo, gov_repo), gov_repo)

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
    return [{
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "citations": json.loads(message.citations or "[]"),
        "usage_log_id": str(message.usage_log_id) if message.usage_log_id else None,
        "created_at": message.created_at,
    } for message in messages]

@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    repo = AIRepository(db)
    conversation = await repo.get_conversation(conversation_id, current_user.id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await repo.delete_conversation(conversation)

@router.post("/ask")
async def ask_question(req: AskRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
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

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    success = await get_ai_service(db).submit_feedback(
        user=current_user,
        log_id=req.ai_usage_log_id,
        rating=req.rating,
        comment=req.comment,
    )
    return {"success": success}
