"""sql_query + describe_schema over the seeded `demo` schema (ADR-008, ADR-033).

Layers, outermost first:
1. sqlglot: exactly one statement; a SELECT (or set operation of SELECTs); no data-modifying
   node anywhere in the tree (INSERT/UPDATE/DELETE/MERGE/DDL/COPY/SELECT INTO/FOR UPDATE, incl.
   inside CTEs); no blocklisted function (set_config, pg_sleep, dblink, large objects, file
   access, backend control).
2. A separate LOGIN role (`SQL_TOOL_ROLE`) with SELECT on `demo` only. It can't read the app
   tables (users, tasks, ...), and it's a different session, so nothing a query does can borrow
   the app's privileges. Why not `SET ROLE` on the app connection: a SELECT can call
   set_config('role', ...) and switch back.
3. The role's defaults: read-only transactions, statement_timeout, idle-in-transaction
   timeout; plus SET LOCAL on every call. Row and cell caps on the result.
"""

from __future__ import annotations

import psycopg
import sqlglot
from pydantic import Field
from sqlglot import exp

from agentops.tools.spec import ToolContext, ToolInput, ToolOutput, ToolSpec

MAX_ROWS = 200
MAX_CELL = 200

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Into,
    exp.Lock,
    exp.Set,
    exp.TruncateTable,
    exp.Grant,
)
_FORBIDDEN_FUNC_PREFIXES = (
    "set_config",
    "pg_sleep",
    "dblink",
    "lo_",
    "pg_read",
    "pg_ls_",
    "pg_stat_file",
    "pg_terminate",
    "pg_cancel",
    "pg_reload",
    "pg_advisory",
    "pg_notify",
    "txid_",
    "current_setting",
)


class SqlRejectedError(ValueError):
    pass


def check_select(query: str) -> None:
    """Raise SqlRejectedError unless `query` is a single, read-only SELECT."""
    try:
        statements = [s for s in sqlglot.parse(query, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise SqlRejectedError(f"could not parse SQL: {str(exc)[:200]}") from None
    if len(statements) != 1:
        raise SqlRejectedError(f"exactly one statement allowed, got {len(statements)}")
    root = statements[0]
    if not isinstance(root, exp.Select | exp.SetOperation):
        raise SqlRejectedError(f"only SELECT is allowed, got {root.key.upper()}")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlRejectedError(f"{node.key.upper()} is not allowed in a query")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name.startswith(_FORBIDDEN_FUNC_PREFIXES):
                raise SqlRejectedError(f"function {name}() is not allowed")


class QueryIn(ToolInput):
    query: str = Field(min_length=1, max_length=4000, description="one PostgreSQL SELECT")
    max_rows: int = Field(default=50, ge=1, le=MAX_ROWS)


class DescribeIn(ToolInput):
    pass


def _cell(v: object) -> str:
    s = "NULL" if v is None else str(v)
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


async def _run(
    ctx: ToolContext, query: str, params: tuple[object, ...], max_rows: int
) -> tuple[list[str], list[tuple[object, ...]], bool]:
    timeout = int(ctx.settings.sql_tool_statement_timeout_ms)
    async with (
        await psycopg.AsyncConnection.connect(
            ctx.settings.sql_tool_dsn, connect_timeout=3, autocommit=False
        ) as conn,
        conn.transaction(),
    ):
        await conn.execute("SET TRANSACTION READ ONLY")
        await conn.execute(f"SET LOCAL statement_timeout = {timeout}")
        cur = await conn.execute(query, params)
        cols = [d.name for d in cur.description or []]
        rows = await cur.fetchmany(max_rows + 1)
    return cols, rows[:max_rows], len(rows) > max_rows


async def sql_query(args: QueryIn, ctx: ToolContext) -> ToolOutput:
    check_select(args.query)
    try:
        cols, rows, more = await _run(ctx, args.query, (), args.max_rows)
    except psycopg.errors.QueryCanceled:
        raise SqlRejectedError(
            f"query cancelled after {ctx.settings.sql_tool_statement_timeout_ms} ms"
        ) from None
    except psycopg.Error as exc:
        raise SqlRejectedError(f"database error: {str(exc).splitlines()[0][:300]}") from None
    lines = ["\t".join(cols)] + ["\t".join(_cell(v) for v in r) for r in rows]
    if more:
        lines.append(f"[... more rows; showing the first {args.max_rows}]")
    return ToolOutput(text="\n".join(lines), data={"columns": cols, "rows": len(rows)})


async def describe_schema(args: DescribeIn, ctx: ToolContext) -> ToolOutput:
    _cols, rows, _ = await _run(
        ctx,
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'demo' ORDER BY table_name, ordinal_position",
        (),
        1000,
    )
    out: dict[str, list[str]] = {}
    for table, col, typ in rows:
        out.setdefault(f"demo.{table}", []).append(f"{col} {typ}")
    text = "\n".join(f"{t}({', '.join(c)})" for t, c in out.items())
    return ToolOutput(text=text or "(no tables visible)")


SQL_TOOLS = [
    ToolSpec(
        name="describe_schema",
        description="List the tables and columns of the demo database (schema `demo`).",
        input_model=DescribeIn,
        fn=describe_schema,
        allowed_agents=frozenset({"analysis"}),
        risk="read",
        timeout_s=10,
        rate_per_min=30,
    ),
    ToolSpec(
        name="sql_query",
        description=(
            "Run ONE read-only PostgreSQL SELECT against the demo database (schema `demo`: "
            "customers, products, orders). Returns tab-separated rows."
        ),
        input_model=QueryIn,
        fn=sql_query,
        allowed_agents=frozenset({"analysis"}),
        risk="read",
        timeout_s=15,
        rate_per_min=30,
    ),
]
