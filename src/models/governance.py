import uuid
from sqlalchemy import ForeignKey, String, Text, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

class PendingDraft(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pending_drafts"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    restructured_body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    restructure_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    restructure_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    restructure_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    page_texts: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    similarity_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    similarity_matches: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    requires_update_confirmation: Mapped[bool] = mapped_column(default=False, nullable=False)
    update_target_article_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    related_article_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
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
