"""Live event streaming: one LISTEN connection per API process fans out to SSE clients.

Ordering/gap handling: a client subscribes BEFORE replaying from the table, so no event can
fall between replay and live. Each notification triggers a fetch of `id > last_seen` for the
task, plus the notified id itself if it is older (an out-of-order commit); ids already sent
are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.observability.events import CHANNEL, TERMINAL_EVENT_TYPES

log = logging.getLogger(__name__)


class EventBroadcaster:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._subs: dict[str, set[asyncio.Queue[int]]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self, ready_timeout: float = 2.0) -> None:
        self._task = asyncio.create_task(self._listen_loop())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), ready_timeout)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _listen_loop(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self._ready.set()
                    self._wake_all()  # after a reconnect, make subscribers re-query
                    async for n in conn.notifies():
                        try:
                            msg = json.loads(n.payload)
                            self._dispatch(msg["task_id"], int(msg["id"]))
                        except (ValueError, KeyError):
                            log.warning("bad notification payload: %r", n.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event listener lost connection; reconnecting")
                await asyncio.sleep(1.0)

    def _dispatch(self, task_id: str, event_id: int) -> None:
        for q in list(self._subs.get(task_id, ())):
            q.put_nowait(event_id)

    def _wake_all(self) -> None:
        for qs in self._subs.values():
            for q in qs:
                q.put_nowait(0)

    @contextlib.contextmanager
    def subscribe(self, task_id: uuid.UUID) -> Any:
        q: asyncio.Queue[int] = asyncio.Queue()
        key = str(task_id)
        self._subs[key].add(q)
        try:
            yield q
        finally:
            self._subs[key].discard(q)
            if not self._subs[key]:
                del self._subs[key]


_SELECT_AFTER = text(
    "SELECT id, type, node, payload, created_at FROM events "
    "WHERE task_id = :t AND id > :after ORDER BY id"
)
_SELECT_ONE = text(
    "SELECT id, type, node, payload, created_at FROM events WHERE task_id = :t AND id = :id"
)


def _to_sse(row: Any) -> dict[str, Any]:
    m = row._mapping
    data = {
        "id": m["id"],
        "type": m["type"],
        "node": m["node"],
        "payload": m["payload"],
        "created_at": m["created_at"].isoformat(),
    }
    return {"id": str(m["id"]), "event": m["type"], "data": json.dumps(data, default=str)}


async def event_stream(
    engine: AsyncEngine,
    broadcaster: EventBroadcaster,
    task_id: uuid.UUID,
    last_event_id: int,
    *,
    idle_timeout: float = 15.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yields SSE dicts. Ends after a terminal event (`task.finished`)."""
    sent: set[int] = set()
    last = last_event_id
    with broadcaster.subscribe(task_id) as q:
        while True:
            async with engine.connect() as conn:
                rows = (await conn.execute(_SELECT_AFTER, {"t": task_id, "after": last})).all()
            for row in rows:
                eid = int(row._mapping["id"])
                if eid in sent:
                    continue
                sent.add(eid)
                last = max(last, eid)
                yield _to_sse(row)
                if row._mapping["type"] in TERMINAL_EVENT_TYPES:
                    return
            # wait for the next notification (or periodically re-check as a safety net)
            try:
                notified = await asyncio.wait_for(q.get(), idle_timeout)
            except TimeoutError:
                continue
            if 0 < notified < last and notified not in sent and notified > last_event_id:
                async with engine.connect() as conn:
                    late = (await conn.execute(_SELECT_ONE, {"t": task_id, "id": notified})).first()
                if late is not None:
                    sent.add(notified)
                    yield _to_sse(late)
                    if late._mapping["type"] in TERMINAL_EVENT_TYPES:
                        return
