"""phase 2: idempotent events, budget ledger, task config

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USD = sa.Numeric(18, 10, asdecimal=False)


def upgrade() -> None:
    # ADR-025: idempotent node events
    op.add_column("events", sa.Column("dedupe_key", sa.String(length=200), nullable=True))
    op.create_index(
        "uq_events_task_dedupe",
        "events",
        ["task_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )

    # what a model call was for (plan, synthesize, review, ...)
    op.add_column("llm_calls", sa.Column("purpose", sa.String(length=32), nullable=True))

    # per-task run options (fake scenario, ...)
    op.add_column(
        "tasks",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # ADR-024: budget ledger
    op.create_table(
        "task_budgets",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("max_usd", USD, nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("used_usd", USD, server_default="0", nullable=False),
        sa.Column("used_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unpriced_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_usd", USD, server_default="0", nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint(
            "reserved_usd >= 0 AND reserved_tokens >= 0 AND reserved_calls >= 0",
            name="ck_task_budgets_reserved_nonneg",
        ),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("usd", USD, nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="reserved", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'orphaned')", name="ck_budget_reservations_status"
        ),
    )
    op.create_index(
        "ix_budget_reservations_open",
        "budget_reservations",
        ["task_id"],
        unique=False,
        postgresql_where=sa.text("status = 'reserved'"),
    )


def downgrade() -> None:
    op.drop_index("ix_budget_reservations_open", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_table("task_budgets")
    op.drop_column("tasks", "config")
    op.drop_column("llm_calls", "purpose")
    op.drop_index("uq_events_task_dedupe", table_name="events")
    op.drop_column("events", "dedupe_key")
