import uuid
from datetime import datetime
from sqlalchemy import ForeignKey, String, Text, Integer, JSON, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

class Connector(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connectors"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    system: Mapped[str] = mapped_column(String(50), nullable=False)  # google_drive, sharepoint
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)  # active, paused
    last_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    company_domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False, default="local")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    jobs: Mapped[list["ConnectorJob"]] = relationship("ConnectorJob", back_populates="connector", cascade="all, delete-orphan")

class ConnectorJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connector_jobs"

    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)  # pending, running, completed, failed
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    connector: Mapped[Connector] = relationship("Connector", back_populates="jobs")

class NotificationQueue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "notification_queue"

    type: Mapped[str] = mapped_column(String(50), nullable=False)  # email, slack
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)  # pending, sent, failed
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class DeadLetterJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "dead_letter_jobs"

    source_queue: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    failed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SearchLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "search_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    user: Mapped["User | None"] = relationship("User")


class ApiRequestMetric(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "api_request_metrics"

    request_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)


class FeatureFlag(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    rollout_percent: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)

class EvalQuestion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "eval_questions"

    question: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expected_answer: Mapped[str] = mapped_column(Text, nullable=False)
    expected_chunk_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of parent chunk IDs
    category: Mapped[str] = mapped_column(String(50), nullable=False)

    runs: Mapped[list["EvalRun"]] = relationship("EvalRun", back_populates="question", cascade="all, delete-orphan")

class EvalRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "eval_runs"

    eval_question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("eval_questions.id", ondelete="CASCADE"), nullable=False)
    retrieval_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    context_recall: Mapped[float] = mapped_column(Float, nullable=False)
    faithfulness: Mapped[float] = mapped_column(Float, nullable=False)
    answer_correctness: Mapped[float] = mapped_column(Float, nullable=False)

    question: Mapped[EvalQuestion] = relationship("EvalQuestion", back_populates="runs")
