"""llm_calls.reservation_id: link each model attempt to its budget reservation

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("reservation_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_llm_calls_reservation",
        "llm_calls",
        "budget_reservations",
        ["reservation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_llm_calls_reservation", "llm_calls", type_="foreignkey")
    op.drop_column("llm_calls", "reservation_id")
