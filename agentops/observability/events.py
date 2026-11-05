"""Event log writer (ADR-006/007/025). Each event is inserted and NOTIFY'd in one transaction,
so listeners are only told about committed rows.

Idempotency (ADR-025): events emitted inside graph nodes carry a `dedupe_key` built from
checkpointed state (see `node_key`). A node that re-runs after a crash or a resume produces
the same key, the insert is a no-op, and no NOTIFY is sent. Events that legitimately repeat
(one per run segment, status changes) pass `dedupe_key=None`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

CHANNEL = "agentops_events"

TERMINAL_EVENT_TYPES = frozenset({"task.finished"})

_INSERT = text(
    "INSERT INTO events (task_id, type, node, payload, dedupe_key) "
    "VALUES (:t, :ty, :n, CAST(:p AS jsonb), :k) "
    "ON CONFLICT (task_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING "
    "RETURNING id"
)


def node_key(node: str, event: str, *parts: object) -> str:
    """Stable dedupe key: node, event type, and the state coordinates that identify one
    execution of the node (plan attempt, wave, subtask id, revision...). Parts that are
    None are skipped. Coordinates must come from checkpointed state, never from clocks,
    pids or random ids, or a re-run would produce a new key."""
    tail = ":".join(str(p) for p in parts if p is not None)
    key = f"{node}:{event}" + (f":{tail}" if tail else "")
    if len(key) > 200:
        raise ValueError(f"dedupe key too long ({len(key)} chars): {key[:80]}...")
    return key


async def emit_on(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    type_: str,
    *,
    node: str | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> int | None:
    """Insert + notify inside the caller's transaction. Returns the new event id, or None
    when an event with the same dedupe_key already exists (nothing inserted or notified)."""
    row = (
        await conn.execute(
            _INSERT,
            {
                "t": task_id,
                "ty": type_,
                "n": node,
                "p": json.dumps(payload or {}, default=str),
                "k": dedupe_key,
            },
        )
    ).first()
    if row is None:
        return None
    event_id = int(row[0])
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
    dedupe_key: str | None = None,
) -> int | None:
    async with engine.begin() as conn:
        return await emit_on(
            conn, task_id, type_, node=node, payload=payload, dedupe_key=dedupe_key
        )
