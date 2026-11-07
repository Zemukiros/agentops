"""SQL tools against the real read-only role (ADR-033). Some tests call `_run` directly to
skip the static check and prove the database layer holds on its own."""

import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.settings import get_settings
from agentops.tools.native import sql
from agentops.tools.spec import ToolContext

pytestmark = pytest.mark.integration


def ctx(engine: AsyncEngine, **overrides: object) -> ToolContext:
    return ToolContext(
        task_id=uuid.uuid4(),
        owner_id=None,
        agent="analysis",
        subtask_id="s",
        revision=0,
        step=1,
        workspace=Path("/nonexistent"),
        settings=get_settings().model_copy(update=overrides),
        engine=engine,
    )


async def test_query_and_describe(engine: AsyncEngine) -> None:
    c = ctx(engine)
    out = await sql.sql_query(
        sql.QueryIn(
            query="SELECT region, count(*) AS n FROM demo.customers GROUP BY region ORDER BY region"
        ),
        c,
    )
    lines = out.text.splitlines()
    assert lines[0] == "region\tn"
    assert sum(int(line.split("\t")[1]) for line in lines[1:]) == 50  # deterministic seed
    schema = (await sql.describe_schema(sql.DescribeIn(), c)).text
    assert "demo.orders(" in schema and "customer_id integer" in schema
    assert "users" not in schema and "tasks" not in schema


async def test_row_cap(engine: AsyncEngine) -> None:
    out = await sql.sql_query(
        sql.QueryIn(query="SELECT id FROM demo.orders", max_rows=5), ctx(engine)
    )
    assert len(out.text.splitlines()) == 1 + 5 + 1 and "more rows" in out.text


async def test_role_cannot_read_app_tables(engine: AsyncEngine) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await sql._run(ctx(engine), "SELECT token_hash FROM public.users", (), 10)
    with pytest.raises(sql.SqlRejectedError, match="permission denied"):
        await sql.sql_query(sql.QueryIn(query="SELECT * FROM public.tasks"), ctx(engine))


async def test_writes_fail_at_the_database_even_without_the_static_check(
    engine: AsyncEngine,
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await sql._run(ctx(engine), "DELETE FROM demo.orders", (), 1)


async def test_role_escape_via_set_config_fails(engine: AsyncEngine) -> None:
    app_user = get_settings().libpq_dsn.split("://", 1)[1].split(":", 1)[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await sql._run(ctx(engine), f"SELECT set_config('role', '{app_user}', false)", (), 1)


async def test_statement_timeout(engine: AsyncEngine) -> None:
    with pytest.raises(psycopg.errors.QueryCanceled):
        await sql._run(ctx(engine, sql_tool_statement_timeout_ms=200), "SELECT pg_sleep(3)", (), 1)
