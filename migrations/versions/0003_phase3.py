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


def downgrade() -> None:
    op.alter_column("llm_calls", "purpose", type_=sa.String(length=32), existing_nullable=True)
