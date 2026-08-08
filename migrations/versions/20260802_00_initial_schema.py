"""baseline schema for fresh and legacy databases

The original project created its schema at application startup. This
migration captures the current SQLAlchemy metadata so a new production
database can be initialized by Alembic alone.
"""
from alembic import op
from src.models import Base

revision = "20260802_00"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # The baseline is intentionally not destructive. Individual migrations
    # own their reversible changes; dropping the entire application schema
    # from a production downgrade is unsafe.
    pass
