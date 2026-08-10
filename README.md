# QNSC Knowledge Base Backend

FastAPI-based backend application for QNSC Knowledge Base, utilizing pgvector for hybrid RAG search, Celery for asynchronous processing, and Redis for caching.

## Getting Started

### Prerequisites
- Python 3.11+
- Poetry
- PostgreSQL with pgvector extension
- Redis

### Installation

1. Copy `.env.example` to `.env` and configure your settings:
   ```bash
   cp .env.example .env
   ```

2. Install the locked dependencies with Poetry:
   ```bash
   poetry install
   ```

3. Run DB migrations:
   ```bash
   poetry run alembic upgrade head
   ```

4. Run the development server:
   ```bash
   poetry run uvicorn src.api.main:app --reload
   ```

### Permission test users

For a development or UAT database, set `SEED_TEST_PASSWORD` in the environment
and run:

```powershell
poetry run python scripts/seed_data.py
```

This creates or refreshes one Admin, CEO, Reviewer, and Staff account in the
`SEED_COMPANY_DOMAIN` tenant. The password is never stored in the repository.
