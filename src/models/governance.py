import uuid
from sqlalchemy import ForeignKey, String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

class PendingDraft(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pending_drafts"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending, approved, rejected
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    creator: Mapped["User | None"] = relationship("User")

class Gap(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "gaps"

    query: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    dept: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # open, assigned, dismissed
    status: Mapped[str] = mapped_column(String(50), default="open", nullable=False)

class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # create, update, delete, permission_change, approve
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)  # article, user, group, draft
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    user: Mapped["User | None"] = relationship("User")
