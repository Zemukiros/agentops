"""phase 5: SSE tickets; replay branches as tasks

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-041: short-lived single-use tickets for EventSource (which can't send headers)
    op.create_table(
        "sse_tickets",
        sa.Column("ticket_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ticket_hash"),
    )
    # ADR-040: a replay branch is a task (its own thread, ledger, events, approvals)
    op.add_column(
        "tasks", sa.Column("kind", sa.String(length=8), server_default="task", nullable=False)
    )
    op.add_column("tasks", sa.Column("parent_task_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("root_task_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("fork_checkpoint_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_tasks_parent", "tasks", "tasks", ["parent_task_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_tasks_root", "tasks", "tasks", ["root_task_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_tasks_root", "tasks", ["root_task_id"])
    op.create_check_constraint("ck_tasks_kind", "tasks", "kind IN ('task', 'replay')")


def downgrade() -> None:
    op.drop_constraint("ck_tasks_kind", "tasks", type_="check")
    op.drop_index("ix_tasks_root", table_name="tasks")
    op.drop_constraint("fk_tasks_root", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_parent", "tasks", type_="foreignkey")
    for col in ("fork_checkpoint_id", "root_task_id", "parent_task_id", "kind"):
        op.drop_column("tasks", col)
    op.drop_table("sse_tickets")
