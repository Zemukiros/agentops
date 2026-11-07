"""phase 4: approvals inbox keys, side-effect detail, memory importance, full tool output

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # approvals: `level` holds the kind (approve_plan, approve_action, escalation, final_review,
    # budget, recursion, side_effect_review). `key` is semantic and stable across re-runs.
    op.add_column("approvals", sa.Column("key", sa.String(length=120), nullable=False))
    op.add_column(
        "approvals",
        sa.Column("source", sa.String(length=8), server_default="graph", nullable=False),
    )
    op.add_column(
        "approvals", sa.Column("blocking", sa.Boolean(), server_default="true", nullable=False)
    )
    op.create_unique_constraint("uq_approvals_task_key", "approvals", ["task_id", "key"])
    op.create_index(
        "ix_approvals_pending_expiry",
        "approvals",
        ["expires_at"],
        postgresql_where=sa.text("status = 'pending' AND blocking"),
    )

    # side_effects: what was executed, and why it failed
    op.add_column("side_effects", sa.Column("tool", sa.String(length=100), nullable=True))
    op.add_column(
        "side_effects",
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("side_effects", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "side_effects",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # memories: outcome must fit every terminal status ("completed_partial" is 17 chars)
    op.alter_column("memories", "outcome", type_=sa.String(length=32), existing_nullable=True)
    # memories: ranking weight
    op.add_column(
        "memories", sa.Column("importance", sa.Float(), server_default="0.5", nullable=False)
    )

    # tool_calls: full (framed, capped) output, so consolidate can screen memories against it
    op.add_column("tool_calls", sa.Column("output_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tool_calls", "output_text")
    op.drop_column("memories", "importance")
    op.alter_column("memories", "outcome", type_=sa.String(length=16), existing_nullable=True)
    for col in ("updated_at", "error", "args", "tool"):
        op.drop_column("side_effects", col)
    op.drop_index("ix_approvals_pending_expiry", table_name="approvals")
    op.drop_constraint("uq_approvals_task_key", "approvals", type_="unique")
    for col in ("blocking", "source", "key"):
        op.drop_column("approvals", col)
