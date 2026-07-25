# QNSC Knowledge Base Backend

FastAPI-based backend application for QNSC Knowledge Base, utilizing pgvector for hybrid RAG search, Celery for asynchronous processing, and Redis for caching.

## Getting Started

### Prerequisites
- Python 3.11+
- PostgreSQL with pgvector extension
- Redis

### Installation

1. Copy `.env.example` to `.env` and configure your settings:
   ```bash
   cp .env.example .env
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run DB migrations:
   ```bash
   alembic upgrade head
   ```

4. Run the development server:
   ```bash
   uvicorn src.api.main:app --reload
   ```
