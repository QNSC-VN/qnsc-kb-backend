from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import settings
from src.api.routers import auth, articles, search, ai, interactions, governance, meta
import structlog

logger = structlog.get_logger()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# Set CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
