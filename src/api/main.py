from contextlib import asynccontextmanager
import asyncio
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import settings
from src.api.routers import auth, articles, search, ai, interactions, governance, meta
from src.api.deps import init_db
import structlog

logger = structlog.get_logger()

async def initialize_resources() -> None:
    logger.info("Starting up and initializing database...")
    try:
        await asyncio.wait_for(init_db(), timeout=10)
        logger.info("Database initialized successfully.")
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
    logger.info("API request started", method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        logger.info(
            "API request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response
    except Exception as exc:
        logger.error(
            "API request failed",
            method=request.method,
            path=request.url.path,
            error=str(exc),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
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

@app.get("/")
async def root():
    return {"message": "Welcome to QNSC Knowledge Base API", "docs": "/docs"}
