"""Postgres token bucket (ADR-006, ADR-030): one atomic statement refills and takes a token.

Capacity = rate_per_min (a burst of one minute's worth); refill = rate_per_min / 60 per second.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_TAKE = text(
    """
    UPDATE rate_limit_buckets SET
        tokens = LEAST(:cap, tokens + EXTRACT(EPOCH FROM clock_timestamp() - updated_at) * :rate)
                 - 1,
        updated_at = clock_timestamp()
    WHERE key = :k
      AND LEAST(:cap, tokens + EXTRACT(EPOCH FROM clock_timestamp() - updated_at) * :rate) >= 1
    RETURNING tokens
    """
)
_SEED = text(
    "INSERT INTO rate_limit_buckets (key, tokens, updated_at) "
    "VALUES (:k, :cap, clock_timestamp()) ON CONFLICT (key) DO NOTHING"
)


async def try_take(engine: AsyncEngine, key: str, rate_per_min: int) -> bool:
    """Take one token from `key`'s bucket. False means rate limited (nothing taken)."""
    cap = float(rate_per_min)
    rate = rate_per_min / 60.0
    async with engine.begin() as conn:
        await conn.execute(_SEED, {"k": key, "cap": cap})
        row = (await conn.execute(_TAKE, {"k": key, "cap": cap, "rate": rate})).first()
    return row is not None
