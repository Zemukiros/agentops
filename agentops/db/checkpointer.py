"""LangGraph Postgres checkpointer wiring.

Verified against langgraph-checkpoint-postgres 3.2.0: AsyncPostgresSaver accepts a psycopg
AsyncConnectionPool; connections need autocommit=True, prepare_threshold=0, row_factory=dict_row
(the same settings `from_conn_string` uses). `setup()` is idempotent (versioned migrations
table) and uses CREATE INDEX CONCURRENTLY, so it must run outside a transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentops.settings import get_settings


@asynccontextmanager
async def open_checkpointer(
    dsn: str | None = None, *, max_size: int = 5
) -> AsyncIterator[AsyncPostgresSaver]:
    pool: AsyncConnectionPool = AsyncConnectionPool(
        dsn or get_settings().libpq_dsn,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    try:
        yield AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    finally:
        await pool.close()


async def setup_checkpointer(dsn: str | None = None) -> None:
    async with open_checkpointer(dsn, max_size=1) as saver:
        await saver.setup()


if __name__ == "__main__":
    asyncio.run(setup_checkpointer())
    print("checkpointer tables ready")
