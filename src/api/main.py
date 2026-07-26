from contextlib import asynccontextmanager
import asyncio
import time
import uuid
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from src.core.config import settings
from src.api.routers import auth, articles, search, ai, interactions, governance, meta, connectors
from src.api.deps import SessionLocal, init_db
from src.domain.events import event_bus
from src.models.article import Article
from src.models.chunk import ArticleChunk
from src.models.ops import ApiRequestMetric
import structlog

logger = structlog.get_logger()


async def record_request_metric(request_id: str, method: str, path: str, status_code: int, duration_ms: float) -> None:
    try:
        async with SessionLocal() as db:
            db.add(ApiRequestMetric(
                request_id=request_id,
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
            ))
            await db.commit()
    except Exception as exc:
        logger.warning("Could not persist API request metric", request_id=request_id, error=str(exc))


async def reconcile_published_indexes() -> None:
    """Repair index state from before persisted indexing status was introduced."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Article.id, Article.index_status, func.count(ArticleChunk.id).label("chunk_count"))
            .outerjoin(ArticleChunk, ArticleChunk.article_id == Article.id)
            .where(Article.status == "published")
            .group_by(Article.id, Article.index_status)
        )
        rows = result.all()
        ready_ids = [article_id for article_id, index_status, chunk_count in rows if index_status == "pending" and chunk_count > 0]
        missing_ids = [article_id for article_id, _, chunk_count in rows if chunk_count == 0]

        for article_id in ready_ids:
            await db.execute(
                Article.__table__.update()
                .where(Article.id == article_id)
                .values(index_status="ready", index_error=None)
            )
        if ready_ids:
            await db.commit()

    for article_id in missing_ids:
        await event_bus.publish("ArticlePublished", {"article_id": str(article_id)})

    if ready_ids or missing_ids:
        logger.info(
            "Published index reconciliation completed",
            marked_ready=len(ready_ids),
            queued_for_indexing=len(missing_ids),
        )

async def initialize_resources() -> None:
    logger.info("Starting up and initializing database...")
    try:
        await asyncio.wait_for(init_db(), timeout=10)
        logger.info("Database initialized successfully.")
        await reconcile_published_indexes()
    except Exception as e:
        logger.error("Failed to initialize database", error=str(e))

    # Embeddings are loaded lazily when indexing/searching. Celery is disabled,
    # so startup should not download or preload the model.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Do not hold the ASGI server hostage while an optional dependency is down
    # or a local embedding model is being downloaded.
    resource_task = asyncio.create_task(initialize_resources())
    yield
    resource_task.cancel()
    try:
        await resource_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# Set CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_logging_middleware(request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    logger.info("API request started", request_id=request_id, method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        logger.info(
            "API request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            request_id=request_id,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        await record_request_metric(
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((time.perf_counter() - started) * 1000, 2),
        )
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception as exc:
        logger.error(
            "API request failed",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
            error=str(exc),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        await record_request_metric(
            request_id,
            request.method,
            request.url.path,
            500,
            round((time.perf_counter() - started) * 1000, 2),
        )
        raise

# Routers
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(articles.router, prefix=f"{settings.API_V1_STR}/articles", tags=["articles"])
app.include_router(search.router, prefix=f"{settings.API_V1_STR}/search", tags=["search"])
app.include_router(ai.router, prefix=f"{settings.API_V1_STR}/ai", tags=["ai"])
app.include_router(interactions.router, prefix=f"{settings.API_V1_STR}/interactions", tags=["interactions"])
app.include_router(governance.router, prefix=f"{settings.API_V1_STR}/governance", tags=["governance"])
app.include_router(meta.router, prefix=f"{settings.API_V1_STR}/meta", tags=["meta"])
app.include_router(connectors.router, prefix=f"{settings.API_V1_STR}/connectors", tags=["connectors"])

@app.get("/")
async def root():
    return {"message": "Welcome to QNSC Knowledge Base API", "docs": "/docs"}


@app.get("/health/live", tags=["system"])
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["system"])
async def health_ready():
    from sqlalchemy import text
    from src.api.deps import engine

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok"}
    except Exception as exc:
        logger.error("Readiness check failed", error=str(exc))
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail={"status": "not_ready", "database": "unavailable"})
