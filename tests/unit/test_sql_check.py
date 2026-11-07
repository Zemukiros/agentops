"""sql_query's static check (ADR-033): one read-only SELECT, nothing else."""

import pytest

from agentops.tools.native.sql import SqlRejectedError, check_select

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select region, count(*) from demo.customers group by region order by 2 desc",
        "WITH t AS (SELECT * FROM demo.orders) SELECT count(*) FROM t",
        "SELECT id FROM demo.products UNION SELECT id FROM demo.customers",
        "SELECT date_trunc('month', order_date), sum(quantity) FROM demo.orders GROUP BY 1",
    ],
)
def test_allowed(query: str) -> None:
    check_select(query)


@pytest.mark.parametrize(
    ("query", "needle"),
    [
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("DELETE FROM demo.orders", "only SELECT"),
        ("UPDATE demo.orders SET quantity = 0", "only SELECT"),
        ("DROP TABLE demo.orders", "only SELECT"),
        ("COPY demo.orders TO '/tmp/x'", "only SELECT"),
        ("WITH d AS (DELETE FROM demo.orders RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT * INTO demo.copy FROM demo.orders", "INTO"),
        ("SELECT * FROM demo.orders FOR UPDATE", "LOCK"),
        ("SELECT set_config('role', 'agentops', false)", "set_config"),
        ("SELECT pg_sleep(60)", "pg_sleep"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT * FROM dblink('host=x', 'select 1') AS t(a int)", "dblink"),
        ("SELECT current_setting('is_superuser')", "current_setting"),
        ("SELEC oops", "only SELECT"),  # sqlglot reads it as an alias; still rejected
        ("SELECT (", "could not parse"),
    ],
)
def test_rejected(query: str, needle: str) -> None:
    with pytest.raises(SqlRejectedError, match=needle):
        check_select(query)
