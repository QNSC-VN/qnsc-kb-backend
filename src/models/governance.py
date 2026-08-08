import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, Integer, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

class PendingDraft(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pending_drafts"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keep the queue tenant-scoped.  Source text is sensitive before it is
    # approved, so deriving tenancy only from an optional creator is unsafe.
    company_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="local", index=True)
    dept: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
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
    # Metadata submitted with a manually authored document.  Uploads retain
    # their source-derived fields above; this lets both paths share one
    # independent-approval workflow without creating a publishable Article
    # before review.
    content_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending, approved, rejected
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_approver_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("external_documents.id", ondelete="SET NULL"), nullable=True, index=True)

    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    assigned_approver: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_approver_id])
    assigner: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_by])
    reviewer: Mapped["User | None"] = relationship("User", foreign_keys=[reviewed_by])


class IngestionFingerprint(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tenant-scoped reservation for a source hash.

    Keeping pending reservations in the database closes the duplicate-upload
    race between two concurrent requests.
    """
    __tablename__ = "ingestion_fingerprints"
    __table_args__ = (UniqueConstraint("company_domain", "source_hash", name="uq_ingestion_fingerprint_tenant_hash"),)

    company_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pending_drafts.id", ondelete="SET NULL"), nullable=True)
    article_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

class Gap(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "gaps"

    __table_args__ = (UniqueConstraint("company_domain", "query", name="uq_gaps_company_query"),)

    company_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="local", index=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
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
