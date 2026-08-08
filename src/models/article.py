import uuid
from datetime import datetime
from typing import Any
from sqlalchemy import Table, Column, ForeignKey, String, Integer, Text, DateTime, JSON, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

# Association table for Article <-> AccessGroup (Many-to-Many)
article_access = Table(
    "article_access",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True),
)

# One article may be visible in several departments. ``articles.dept`` is
# retained as the primary/legacy department for synchronized integrations.
article_departments = Table(
    "article_departments",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column("department_id", ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True),
)

class Article(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("company_domain", "external_id", name="uq_articles_company_external_id"),)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    dept: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    company_domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False, default="local")
    # POLICY, SOP, DECISION, FAQ, RCA, HOWTO, PLAYBOOK, REFERENCE
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # public, internal, confidential, restricted
    sensitivity: Mapped[str] = mapped_column(String(50), default="internal", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # draft, pending_review, published, archived
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    related_article_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_reviewed: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_review: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needs_update: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    index_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped["User | None"] = relationship("User")
    access_groups: Mapped[list["AccessGroup"]] = relationship(
        "AccessGroup", secondary=article_access
    )
    departments: Mapped[list["Department"]] = relationship(
        "Department", secondary=article_departments, lazy="selectin"
    )
    versions: Mapped[list["ArticleVersion"]] = relationship(
        "ArticleVersion", back_populates="article", cascade="all, delete-orphan"
    )
    sources: Mapped[list["DocumentSource"]] = relationship(
        "DocumentSource", back_populates="article", cascade="all, delete-orphan"
    )
    tags: Mapped[list["ArticleTag"]] = relationship(
        "ArticleTag", back_populates="article", cascade="all, delete-orphan"
    )

    @property
    def source_available(self) -> bool:
        return any(source.storage_key for source in self.sources)

class ArticleVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "article_versions"
    __table_args__ = (UniqueConstraint("article_id", "version", name="uq_article_versions_article_version"),)

    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    edited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    article: Mapped[Article] = relationship("Article", back_populates="versions")
    editor: Mapped["User | None"] = relationship("User")

class ArticleTag(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "article_tags"
    __table_args__ = (
        UniqueConstraint("article_id", "tag", name="uq_article_tag"),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), nullable=False)
    tag: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    article: Mapped[Article] = relationship("Article", back_populates="tags")

class DocumentSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "document_sources"

    article_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)  # google_drive, sharepoint, manual
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    page_texts: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    article: Mapped[Article | None] = relationship("Article", back_populates="sources")
