"""phase 3: tools (rate limits, tool_calls detail), demo SQL schema + read-only role

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentops.settings import get_settings

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

    # ADR-033: demo dataset for sql_query / describe_schema, and the read-only role
    _create_demo_schema()
    _create_readonly_role()


def _create_demo_schema() -> None:
    """Small synthetic dataset, deterministic (no random()), so results are reproducible."""
    op.execute("CREATE SCHEMA demo")
    op.execute(
        """
        CREATE TABLE demo.customers (
            id integer PRIMARY KEY,
            name text NOT NULL,
            region text NOT NULL,
            signup_date date NOT NULL
        );
        CREATE TABLE demo.products (
            id integer PRIMARY KEY,
            name text NOT NULL,
            category text NOT NULL,
            unit_price numeric(10, 2) NOT NULL
        );
        CREATE TABLE demo.orders (
            id integer PRIMARY KEY,
            customer_id integer NOT NULL REFERENCES demo.customers(id),
            product_id integer NOT NULL REFERENCES demo.products(id),
            quantity integer NOT NULL,
            order_date date NOT NULL
        );
        INSERT INTO demo.customers
        SELECT i,
               'Customer ' || i,
               (ARRAY['North', 'South', 'East', 'West'])[1 + (i * 7) % 4],
               DATE '2025-01-01' + ((i * 11) % 365)
        FROM generate_series(1, 50) AS i;
        INSERT INTO demo.products
        SELECT i,
               'Product ' || i,
               (ARRAY['hardware', 'software', 'services'])[1 + (i * 5) % 3],
               (10 + (i * 13) % 90)::numeric(10, 2)
        FROM generate_series(1, 20) AS i;
        INSERT INTO demo.orders
        SELECT i,
               1 + (i * 37) % 50,
               1 + (i * 17) % 20,
               1 + (i * 3) % 5,
               DATE '2026-01-01' + ((i * 29) % 300)
        FROM generate_series(1, 500) AS i;
        """
    )


def _create_readonly_role() -> None:
    s = get_settings()
    role = s.sql_tool_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid SQL_TOOL_ROLE {role!r}")
    password = s.sql_tool_password.get_secret_value().replace("'", "''")
    timeout = int(s.sql_tool_statement_timeout_ms)
    # Roles are cluster-wide (the test database shares the cluster), so create-or-update.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{password}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
                    CONNECTION LIMIT 10;
            ELSE
                ALTER ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 10;
            END IF;
        END
        $$;
        ALTER ROLE {role} SET default_transaction_read_only = on;
        ALTER ROLE {role} SET statement_timeout = {timeout};
        ALTER ROLE {role} SET idle_in_transaction_session_timeout = 10000;
        """
    )
    op.execute(
        f"""
        DO $$ BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {role}', current_database());
        END $$;
        REVOKE ALL ON SCHEMA public FROM {role};
        GRANT USAGE ON SCHEMA demo TO {role};
        GRANT SELECT ON ALL TABLES IN SCHEMA demo TO {role};
        """
    )


def downgrade() -> None:
    role = get_settings().sql_tool_role
    op.execute(
        f"""
        REVOKE ALL ON ALL TABLES IN SCHEMA demo FROM {role};
        REVOKE ALL ON SCHEMA demo FROM {role};
        DO $$ BEGIN
            EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {role}', current_database());
        END $$;
        DROP SCHEMA demo CASCADE;
        """
    )
    # The role itself is cluster-wide and may hold grants in other databases: left in place.
    op.drop_table("rate_limit_buckets")
    for col in ("tool_call_id", "step", "revision", "source", "status"):
        op.drop_column("tool_calls", col)
    op.alter_column("llm_calls", "purpose", type_=sa.String(length=32), existing_nullable=True)
