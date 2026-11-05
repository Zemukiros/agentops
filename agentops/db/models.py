"""SQLAlchemy ORM models. Migrations `0001_initial` + `0002_phase2` mirror this file."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentops.settings import EMBEDDING_DIM


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }


def _enum(e: type[StrEnum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


class UserRole(StrEnum):
    USER = "user"
    REVIEWER = "reviewer"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    # terminal states (DECISIONS.md invariants): every run ends in exactly one of these
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED_PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
        TaskStatus.BUDGET_EXCEEDED,
    }
)


class JobKind(StrEnum):
    RUN = "run"
    RESUME = "resume"
    MAINTENANCE = "maintenance"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    TAKEN_OVER = "taken_over"
    EXPIRED = "expired"


class SideEffectStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"))
    created_at: Mapped[datetime] = _now()


class Task(Base):
    """A user request. `id` is also the LangGraph `thread_id`."""

    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    request: Mapped[str] = mapped_column(Text)
    graph: Mapped[str] = mapped_column(String(50), server_default="main")
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), server_default=TaskStatus.QUEUED.value
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-task run options, e.g. {"scenario": "revise_once"} (fake mode only).
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (Index("ix_tasks_owner_created", "owner_id", "created_at"),)


_USD = Numeric(12, 6, asdecimal=False)


class TaskBudget(Base):
    """Budget ledger, one row per task (ADR-024). Source of truth for spend.

    Invariant (enforced by the reserve UPDATE's WHERE clause and CHECK constraints):
    used + reserved <= max, for usd, tokens and calls.
    """

    __tablename__ = "task_budgets"
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    max_usd: Mapped[float] = mapped_column(_USD)
    max_tokens: Mapped[int] = mapped_column(Integer)
    max_calls: Mapped[int] = mapped_column(Integer)
    used_usd: Mapped[float] = mapped_column(_USD, server_default="0")
    used_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    used_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    unpriced_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved_usd: Mapped[float] = mapped_column(_USD, server_default="0")
    reserved_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint(
            "used_usd + reserved_usd <= max_usd AND used_tokens + reserved_tokens <= max_tokens "
            "AND used_calls + reserved_calls <= max_calls",
            name="ck_task_budgets_within_limits",
        ),
        CheckConstraint(
            "reserved_usd >= 0 AND reserved_tokens >= 0 AND reserved_calls >= 0",
            name="ck_task_budgets_reserved_nonneg",
        ),
    )


class BudgetReservation(Base):
    """Worst-case hold for one model attempt; settled with actual usage afterwards."""

    __tablename__ = "budget_reservations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    usd: Mapped[float] = mapped_column(_USD)
    tokens: Mapped[int] = mapped_column(Integer)
    calls: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="reserved")
    created_at: Mapped[datetime] = _now()
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'settled', 'released')", name="ck_budget_reservations_status"
        ),
        Index(
            "ix_budget_reservations_open",
            "task_id",
            postgresql_where=text("status = 'reserved'"),
        ),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind, "job_kind"))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), server_default=JobStatus.QUEUED.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_after: Mapped[datetime] = _now()
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index(
            "ix_jobs_claimable",
            "status",
            "run_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_jobs_thread", "thread_id"),
    )


class Event(Base):
    """Append-only event log; `id` is the SSE event id (monotonic)."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # Stable key for events emitted inside graph nodes, so a re-run node cannot duplicate
    # them (ADR-025). NULL for events that legitimately repeat (e.g. run.segment_started).
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        Index("ix_events_task_id_id", "task_id", "id"),
        Index(
            "uq_events_task_dedupe",
            "task_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )


class LLMCall(Base):
    """One row per model attempt, including retries and structured-output re-asks (ADR-007)."""

    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    attempt: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_table_version: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ix_llm_calls_task", "task_id"),)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    subtask_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(100))
    risk: Mapped[str] = mapped_column(String(16))
    args: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    ok: Mapped[bool] = mapped_column(Boolean)
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (Index("ix_tool_calls_task", "task_id"),)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(32))
    interrupt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), server_default=ApprovalStatus.PENDING.value
    )
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    decision: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_approvals_status", "status", "created_at"),)


class SideEffect(Base):
    """ADR-004: idempotency guard for every external action."""

    __tablename__ = "side_effects"
    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    action_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[SideEffectStatus] = mapped_column(_enum(SideEffectStatus, "side_effect_status"))
    result: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Memory(Base):
    """Long-term semantic memory (ADR-005). Always queried with `WHERE user_id = :uid`."""

    __tablename__ = "memories"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # episode | lesson | preference | fact
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    superseded: Mapped[bool] = mapped_column(Boolean, server_default="false")
    access_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = _now()
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        Index("ix_memories_user", "user_id", "superseded"),
        Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
