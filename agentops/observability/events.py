"""Event log writer (ADR-006/007). Each event is inserted and NOTIFY'd in one transaction,
so listeners are only told about committed rows."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

CHANNEL = "agentops_events"

TERMINAL_EVENT_TYPES = frozenset({"task.finished"})


async def emit_on(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    type_: str,
    *,
    node: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert + notify inside the caller's transaction. Returns the event id."""
    row = await conn.execute(
        text(
            "INSERT INTO events (task_id, type, node, payload) "
            "VALUES (:t, :ty, :n, CAST(:p AS jsonb)) RETURNING id"
        ),
        {"t": task_id, "ty": type_, "n": node, "p": json.dumps(payload or {}, default=str)},
    )
    event_id = int(row.scalar_one())
    await conn.execute(
        text("SELECT pg_notify(:c, :m)"),
        {"c": CHANNEL, "m": json.dumps({"task_id": str(task_id), "id": event_id})},
    )
    return event_id


async def emit(
    engine: AsyncEngine,
    task_id: uuid.UUID,
    type_: str,
    *,
    node: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    async with engine.begin() as conn:
        return await emit_on(conn, task_id, type_, node=node, payload=payload)
