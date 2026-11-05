"""Per-thread mutual exclusion with Postgres session advisory locks (ADR-006).

The lock is held on a dedicated AUTOCOMMIT connection for the whole run segment. If the worker
process dies, Postgres drops the connection and releases the lock automatically, so a crashed
worker can never block its thread forever.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Namespaced key: hashtextextended(text, seed) -> bigint; seed separates us from other users.
_LOCK_SEED = 0x4167_656E  # "Agen"


@asynccontextmanager
async def thread_lock(engine: AsyncEngine, thread_id: uuid.UUID) -> AsyncIterator[bool]:
    """Yields True if this process now owns the thread, False if another process does."""
    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        params = {"k": str(thread_id), "s": _LOCK_SEED}
        got = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:k, :s))"), params
            )
        ).scalar_one()
        try:
            yield bool(got)
        finally:
            if got:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:k, :s))"), params
                    )
                except BaseException:
                    # Never return a connection that might still hold the lock to the pool.
                    await raw.invalidate()
                    raise
