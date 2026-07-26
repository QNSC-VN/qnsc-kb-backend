import uuid
import json
from typing import Any
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.deps import get_db, get_current_user, require_role
from src.models import User
from src.repositories.governance import GovernanceRepository
from src.repositories.article import ArticleRepository
from src.repositories.ops import OpsRepository
from src.domain.governance import GovernanceService
from src.domain.ai_service import AIService
from src.domain.search_service import SearchService
from src.repositories.chunk import ChunkRepository
from src.repositories.ai import AIRepository
from src.models.ops import EvalQuestion, EvalRun
from src.rag.evaluator import answer_correctness, context_recall, lexical_faithfulness
from src.core.config import settings
from src.models.ops import FeatureFlag
from src.repositories.feature_flags import FeatureFlagRepository
from src.domain.review import ReviewService

router = APIRouter()

class ApproveRequest(BaseModel):
    type: str = "SOP"  # POLICY, SOP, FAQ
    dept: str = "Engineering"
    update_article_id: uuid.UUID | None = None
    treat_as_new: bool = False

class AssignRequest(BaseModel):
    dept: str


class EvalQuestionCreate(BaseModel):
    question: str
    expected_answer: str
    expected_chunk_ids: list[str] = []
    category: str = "general"


class FeatureFlagUpdate(BaseModel):
    enabled: bool = True
    rollout_percent: int = 100
    role: str | None = None
    department: str | None = None

MANAGED_FEATURE_FLAGS = {
    "ai.document_restructure": {
        "label": "AI document reading view",
        "description": "Restructure uploaded content into a lossless Markdown reading view before indexing.",
        "default_enabled": settings.RESTRUCTURE_ENABLED,
    },
}

@router.post("/reviews/verify")
async def verify_review_deadlines(
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Run the local review scan while the Celery worker is intentionally disabled."""
    overdue_ids = await ReviewService(ArticleRepository(db)).verify_review_deadlines()
    return {"overdue_article_ids": overdue_ids, "count": len(overdue_ids)}

@router.get("/pending-drafts")
async def list_pending_drafts(
    status: str | None = Query(None),
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    drafts = await service.list_drafts(status)
    return [
        {
            "id": str(draft.id),
            "title": draft.title,
            "source_ref": draft.source_ref,
            "source_hash": draft.source_hash,
            "summary": draft.summary,
            "restructured_body_md": draft.restructured_body_md,
            "restructure_status": draft.restructure_status,
            "restructure_model": draft.restructure_model,
            "restructure_error": draft.restructure_error,
            "status": draft.status,
            "created_by": str(draft.created_by) if draft.created_by else None,
            "created_at": draft.created_at,
            "similarity_level": draft.similarity_level,
            "similarity_matches": draft.similarity_matches or [],
            "requires_update_confirmation": draft.requires_update_confirmation,
            "related_article_ids": draft.related_article_ids or [],
            "tags": draft.tags or [],
        }
        for draft in drafts
    ]

@router.post("/pending-drafts/{id}/approve")
async def approve_draft(
    id: uuid.UUID,
    req: ApproveRequest,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    article = await service.approve_draft(
        user=current_user,
        draft_id=id,
        category=req.type,
        dept=req.dept
        ,update_article_id=req.update_article_id,
        treat_as_new=req.treat_as_new,
    )
    return {"id": str(article.id), "title": article.title, "status": article.status, "version": article.version}

@router.post("/pending-drafts/{id}/reject")
async def reject_draft(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    draft = await service.reject_draft(current_user, id)
    return {"id": str(draft.id), "title": draft.title, "status": draft.status}

@router.post("/pending-drafts/{id}/restructure")
async def restructure_draft(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    service = GovernanceService(GovernanceRepository(db), ArticleRepository(db))
    enabled = settings.RESTRUCTURE_ENABLED and await FeatureFlagRepository(db).is_enabled("ai.document_restructure", current_user)
    draft = await service.restructure_draft(current_user, id, enabled=enabled)
    return {
        "id": str(draft.id),
        "restructured_body_md": draft.restructured_body_md,
        "restructure_status": draft.restructure_status,
        "restructure_model": draft.restructure_model,
        "restructure_error": draft.restructure_error,
    }

@router.get("/pending-drafts/{id}/comparison")
async def compare_pending_draft(
    id: uuid.UUID,
    article_id: uuid.UUID = Query(...),
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    draft = await GovernanceRepository(db).get_draft(id)
    if not draft or draft.status != "pending":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Pending draft not found")
    article = await ArticleRepository(db).get_by_id(article_id)
    if not article or article.status == "deleted":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Comparison article not found")
    if current_user.role != "Admin" and article.company_domain != current_user.company_domain:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="The comparison article is outside your company")
    return {
        "id": str(article.id),
        "title": article.title,
        "body_md": article.body_md,
        "version": article.version,
        "status": article.status,
        "lifecycle_status": article.lifecycle_status,
    }

@router.get("/gaps")
async def list_search_gaps(
    status: str | None = Query(None),
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.list_gaps(status)

@router.post("/gaps/{id}/assign")
async def assign_gap(
    id: uuid.UUID,
    req: AssignRequest,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.assign_gap(current_user, id, req.dept)

@router.post("/gaps/{id}/dismiss")
async def dismiss_gap(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.dismiss_gap(current_user, id)

@router.get("/audit-log")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    current_user: User = Depends(require_role(["Admin"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.list_audit_logs(current_user, limit)

@router.get("/health-metrics")
async def get_health_metrics(
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    gov_repo = GovernanceRepository(db)
    art_repo = ArticleRepository(db)
    service = GovernanceService(gov_repo, art_repo)
    return await service.get_dashboard_metrics(current_user)

@router.get("/eval-runs")
async def get_eval_runs(
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db)
) -> Any:
    ops_repo = OpsRepository(db)
    return await ops_repo.list_eval_runs()


@router.get("/eval-questions")
async def list_eval_questions(
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    questions = await OpsRepository(db).list_eval_questions()
    return [{
        "id": str(item.id),
        "question": item.question,
        "expected_answer": item.expected_answer,
        "expected_chunk_ids": json.loads(item.expected_chunk_ids or "[]"),
        "category": item.category,
    } for item in questions]


@router.post("/eval-questions", status_code=status.HTTP_201_CREATED)
async def create_eval_question(
    req: EvalQuestionCreate,
    current_user: User = Depends(require_role(["Admin"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    item = await OpsRepository(db).create_eval_question(EvalQuestion(
        question=req.question,
        expected_answer=req.expected_answer,
        expected_chunk_ids=json.dumps(req.expected_chunk_ids),
        category=req.category,
    ))
    return {"id": str(item.id), "question": item.question, "category": item.category}


@router.post("/eval-questions/{id}/run")
async def run_eval_question(
    id: uuid.UUID,
    current_user: User = Depends(require_role(["Admin", "CEO", "Reviewer", "Department Owner"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    ops_repo = OpsRepository(db)
    question = await ops_repo.get_eval_question(id)
    if not question:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Evaluation question not found")

    gov_repo = GovernanceRepository(db)
    search_service = SearchService(ChunkRepository(db), gov_repo)
    retrieved = await search_service.search(current_user, question.question, limit=10)
    expected_ids = json.loads(question.expected_chunk_ids or "[]")
    retrieval_score = context_recall([item["chunk_id"] for item in retrieved], expected_ids)
    context = "\n".join(item["parent_text"] for item in retrieved)
    answer = await AIService(AIRepository(db), search_service, gov_repo).ask(current_user, question.question)
    faithfulness_score = lexical_faithfulness(answer["answer"], context)
    correctness_score = answer_correctness(answer["answer"], question.expected_answer)
    run = await ops_repo.create_eval_run(EvalRun(
        eval_question_id=question.id,
        retrieval_version=settings.RETRIEVAL_VERSION,
        prompt_version=settings.PROMPT_VERSION,
        context_recall=retrieval_score,
        faithfulness=faithfulness_score,
        answer_correctness=correctness_score,
    ))
    return {
        "id": str(run.id),
        "eval_question_id": str(question.id),
        "context_recall": retrieval_score,
        "faithfulness": faithfulness_score,
        "answer_correctness": correctness_score,
        "created_at": run.created_at,
    }


@router.get("/feature-flags")
async def list_feature_flags(
    current_user: User = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    flags = {flag.key: flag for flag in await FeatureFlagRepository(db).list() if flag.key in MANAGED_FEATURE_FLAGS}
    response = [{
        "id": str(flag.id),
        "key": flag.key,
        "enabled": flag.enabled,
        "rollout_percent": flag.rollout_percent,
        "role": flag.role,
        "department": flag.department,
        "label": MANAGED_FEATURE_FLAGS.get(flag.key, {}).get("label", flag.key),
        "description": MANAGED_FEATURE_FLAGS.get(flag.key, {}).get("description", ""),
    } for flag in flags.values()]
    for key, metadata in MANAGED_FEATURE_FLAGS.items():
        if key not in flags:
            response.append({
                "id": None,
                "key": key,
                "enabled": metadata["default_enabled"],
                "rollout_percent": 100,
                "role": None,
                "department": None,
                "label": metadata["label"],
                "description": metadata["description"],
            })
    return sorted(response, key=lambda item: item["key"])


@router.put("/feature-flags/{key}")
async def update_feature_flag(
    key: str,
    req: FeatureFlagUpdate,
    current_user: User = Depends(require_role(["Admin", "CEO"])),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if not 0 <= req.rollout_percent <= 100:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="rollout_percent must be between 0 and 100")
    if key not in MANAGED_FEATURE_FLAGS and current_user.role != "Admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="CEOs can only manage approved company features")
    flag = await FeatureFlagRepository(db).upsert(key, req.enabled, req.rollout_percent, req.role, req.department)
    return {
        "id": str(flag.id),
        "key": flag.key,
        "enabled": flag.enabled,
        "rollout_percent": flag.rollout_percent,
        "role": flag.role,
        "department": flag.department,
        "label": MANAGED_FEATURE_FLAGS.get(flag.key, {}).get("label", flag.key),
        "description": MANAGED_FEATURE_FLAGS.get(flag.key, {}).get("description", ""),
    }
