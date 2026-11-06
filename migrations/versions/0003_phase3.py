"""phase 3: tools (rate limits, tool_calls detail), demo SQL schema + read-only role

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # qualified purposes, e.g. "review/r2/<32-char subtask id>" (ADR-027)
    op.alter_column("llm_calls", "purpose", type_=sa.String(length=64), existing_nullable=True)

    # ADR-030: tool call detail
    op.add_column(
        "tool_calls", sa.Column("status", sa.String(length=20), server_default="ok", nullable=False)
    )
    op.add_column(
        "tool_calls",
        sa.Column("source", sa.String(length=8), server_default="native", nullable=False),
    )
    op.add_column("tool_calls", sa.Column("revision", sa.Integer(), nullable=True))
    op.add_column("tool_calls", sa.Column("step", sa.Integer(), nullable=True))
    op.add_column("tool_calls", sa.Column("tool_call_id", sa.String(length=100), nullable=True))

    # ADR-030: Postgres token-bucket rate limiter
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_buckets")
    for col in ("tool_call_id", "step", "revision", "source", "status"):
        op.drop_column("tool_calls", col)
    op.alter_column("llm_calls", "purpose", type_=sa.String(length=32), existing_nullable=True)
