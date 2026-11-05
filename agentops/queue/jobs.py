"""Postgres job queue (ADR-006).

- `claim` takes one job with `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the
  same row. A job is claimable when it is queued, or running with an expired lease (its worker
  died). Claiming bumps `attempts` and sets a lease.
- The worker must `heartbeat` before the lease expires. Heartbeat/complete/fail only succeed for
  the current `lease_owner`, so a worker that lost its lease cannot clobber the new owner.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

MAX_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    thread_id: uuid.UUID | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: datetime


async def enqueue_on(
    conn: AsyncConnection,
    kind: str,
    *,
    thread_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 5,
    delay_seconds: float = 0.0,
) -> int:
    row = await conn.execute(
        text(
            "INSERT INTO jobs (kind, thread_id, payload, max_attempts, run_after) "
            "VALUES (CAST(:k AS job_kind), :t, CAST(:p AS jsonb), :m, "
            "now() + make_interval(secs => :d)) RETURNING id"
        ),
        {
            "k": kind,
            "t": thread_id,
            "p": json.dumps(payload or {}),
            "m": max_attempts,
            "d": delay_seconds,
        },
    )
    return int(row.scalar_one())


async def enqueue(engine: AsyncEngine, kind: str, **kw: Any) -> int:
    async with engine.begin() as conn:
        return await enqueue_on(conn, kind, **kw)


_CLAIM_SQL = text(
    """
    UPDATE jobs SET
        status = 'running',
        lease_owner = :owner,
        lease_expires_at = now() + make_interval(secs => :lease),
        heartbeat_at = now(),
        attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
          AND run_after <= now()
          AND attempts < max_attempts
        ORDER BY run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, thread_id, payload, attempts, max_attempts, lease_owner, lease_expires_at
    """
)

# Jobs whose worker died on their last allowed attempt: never claimable again, so fail them.
_REAP_SQL = text(
    """
    UPDATE jobs SET status = 'failed', finished_at = now(),
        last_error = coalesce(last_error, '') || 'lease expired on final attempt'
    WHERE status = 'running' AND lease_expires_at < now() AND attempts >= max_attempts
    RETURNING id, thread_id
    """
)


async def claim(engine: AsyncEngine, owner: str, lease_seconds: float) -> ClaimedJob | None:
    async with engine.begin() as conn:
        row = (await conn.execute(_CLAIM_SQL, {"owner": owner, "lease": lease_seconds})).first()
    if row is None:
        return None
    m = row._mapping
    return ClaimedJob(
        id=int(m["id"]),
        kind=str(m["kind"]),
        thread_id=m["thread_id"],
        payload=dict(m["payload"]),
        attempts=int(m["attempts"]),
        max_attempts=int(m["max_attempts"]),
        lease_owner=str(m["lease_owner"]),
        lease_expires_at=m["lease_expires_at"],
    )


async def reap_exhausted(engine: AsyncEngine) -> list[tuple[int, uuid.UUID | None]]:
    async with engine.begin() as conn:
        rows = (await conn.execute(_REAP_SQL)).all()
    return [(int(r[0]), r[1]) for r in rows]


async def heartbeat(engine: AsyncEngine, job_id: int, owner: str, lease_seconds: float) -> bool:
    """Extend the lease. False means this worker no longer owns the job."""
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "UPDATE jobs SET heartbeat_at = now(), "
                "lease_expires_at = now() + make_interval(secs => :lease) "
                "WHERE id = :id AND lease_owner = :owner AND status = 'running' RETURNING id"
            ),
            {"id": job_id, "owner": owner, "lease": lease_seconds},
        )
        return row.first() is not None


async def complete(engine: AsyncEngine, job_id: int, owner: str) -> bool:
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "UPDATE jobs SET status = 'done', finished_at = now(), lease_expires_at = NULL "
                "WHERE id = :id AND lease_owner = :owner AND status = 'running' RETURNING id"
            ),
            {"id": job_id, "owner": owner},
        )
        return row.first() is not None


def backoff_seconds(attempts: int, base: float = 2.0) -> float:
    return float(min(MAX_BACKOFF_SECONDS, base**attempts))


async def fail(engine: AsyncEngine, job_id: int, owner: str, error: str) -> str | None:
    """Record a failure. Returns the new status ('queued' for retry, 'failed' if exhausted),
    or None if this worker no longer owns the job."""
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                """
                UPDATE jobs SET
                    status = CASE WHEN attempts >= max_attempts
                                  THEN 'failed'::job_status ELSE 'queued'::job_status END,
                    finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                    run_after = now() + make_interval(secs => LEAST(:cap, power(2, attempts))),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = :err
                WHERE id = :id AND lease_owner = :owner AND status = 'running'
                RETURNING status
                """
            ),
            {"id": job_id, "owner": owner, "err": error[:4000], "cap": MAX_BACKOFF_SECONDS},
        )
        r = row.first()
        return None if r is None else str(r[0])


async def release(engine: AsyncEngine, job_id: int, owner: str, delay_seconds: float) -> bool:
    """Give a claimed job back without counting the attempt (e.g. thread lock busy)."""
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "UPDATE jobs SET status = 'queued', attempts = attempts - 1, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "run_after = now() + make_interval(secs => :d) "
                "WHERE id = :id AND lease_owner = :owner AND status = 'running' RETURNING id"
            ),
            {"id": job_id, "owner": owner, "d": delay_seconds},
        )
        return row.first() is not None
