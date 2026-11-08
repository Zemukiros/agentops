"""ADR-041: short-lived, single-use SSE tickets.

EventSource cannot send an Authorization header, and a bearer token in a URL leaks into logs,
history and Referer. So an authenticated client asks for a ticket bound to (user, task); the
stream URL carries the ticket; redeeming it is one atomic UPDATE, so it works exactly once,
before it expires, for that task only. Only the HMAC of the ticket is stored.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.api.auth import hash_token

TICKET_TTL = timedelta(seconds=60)

_REDEEM = text(
    "UPDATE sse_tickets SET used_at = now() "
    "WHERE ticket_hash = :h AND task_id = :t AND used_at IS NULL AND expires_at > now() "
    "RETURNING user_id"
)


async def issue(
    engine: AsyncEngine, user_id: uuid.UUID, task_id: uuid.UUID
) -> tuple[str, datetime]:
    ticket = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + TICKET_TTL
    async with engine.begin() as c:
        # opportunistic cleanup keeps the table small
        await c.execute(
            text("DELETE FROM sse_tickets WHERE expires_at < now() - interval '1 hour'")
        )
        await c.execute(
            text(
                "INSERT INTO sse_tickets (ticket_hash, user_id, task_id, expires_at) "
                "VALUES (:h, :u, :t, :e)"
            ),
            {"h": hash_token(ticket), "u": user_id, "t": task_id, "e": expires},
        )
    return ticket, expires


async def redeem(engine: AsyncEngine, ticket: str, task_id: uuid.UUID) -> uuid.UUID | None:
    """The ticket's user id, or None if unknown, used, expired, or for another task."""
    async with engine.begin() as c:
        row = (await c.execute(_REDEEM, {"h": hash_token(ticket), "t": task_id})).first()
    return row[0] if row else None
