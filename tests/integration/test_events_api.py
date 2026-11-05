import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.api.auth import require_role
from agentops.api.main import create_app
from agentops.db.models import UserRole
from agentops.observability.events import CHANNEL, emit, node_key
from agentops.settings import Settings, get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


@pytest.fixture
async def app(test_db: str) -> AsyncIterator[FastAPI]:
    application = create_app(get_settings())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=20) as c:
        yield c


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
        if data:
            events.append(json.loads("".join(data)))
    return events


async def test_emit_inserts_and_notifies(engine: AsyncEngine, alice: SeededUser) -> None:
    tid = await create_task(engine, alice)
    dsn = get_settings().libpq_dsn
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        await c.execute(f"LISTEN {CHANNEL}")

        async def first_notification() -> psycopg.Notify:
            async for n in c.notifies(timeout=5, stop_after=1):
                return n
            raise AssertionError("no notification within 5s")

        waiter = asyncio.create_task(first_notification())
        await asyncio.sleep(0.1)  # consumer is now reading the socket
        eid = await emit(engine, tid, "node.completed", node="x", payload={"k": 1})
        n = await asyncio.wait_for(waiter, 10)
    assert json.loads(n.payload) == {"task_id": str(tid), "id": eid}
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT type, node, payload FROM events"))).one()
    assert (row.type, row.node, row.payload) == ("node.completed", "x", {"k": 1})


async def test_health_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "checkpointer": "ok"}


async def test_health_503_when_db_unreachable() -> None:
    bad = Settings(database_url=SecretStr("postgresql+psycopg://x:y@127.0.0.1:1/none"))
    application = create_app(bad)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"].startswith("error")
    assert body["checks"]["checkpointer"].startswith("error")


async def test_auth_required_and_role(
    client: httpx.AsyncClient, alice: SeededUser, reviewer: SeededUser
) -> None:
    assert (await client.get("/me")).status_code == 401
    bad = await client.get("/me", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    me = await client.get("/me", headers=alice.headers)
    assert me.json()["role"] == "user"
    assert (await client.get("/me", headers=reviewer.headers)).json()["role"] == "reviewer"


async def test_require_role_dependency(
    app: FastAPI, alice: SeededUser, reviewer: SeededUser
) -> None:
    r = APIRouter()

    @r.get("/_reviewer_only", dependencies=[require_role(UserRole.REVIEWER)])
    async def only() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(r)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/_reviewer_only", headers=alice.headers)).status_code == 403
        assert (await c.get("/_reviewer_only", headers=reviewer.headers)).status_code == 200


async def test_create_task_enqueues_job_and_owner_isolation(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser, bob: SeededUser
) -> None:
    r = await client.post("/tasks", json={"request": "probe"}, headers=alice.headers)
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["status"] == "queued" and task["graph"] == "demo"
    async with engine.connect() as c:
        job = (await c.execute(text("SELECT kind, thread_id FROM jobs"))).one()
        ev = (await c.execute(text("SELECT type FROM events"))).scalars().all()
    assert job.kind == "run" and str(job.thread_id) == task["id"]
    assert ev == ["task.created"]

    assert (await client.get(f"/tasks/{task['id']}", headers=alice.headers)).status_code == 200
    # another user's task is invisible (404, not 403) on every task route
    assert (await client.get(f"/tasks/{task['id']}", headers=bob.headers)).status_code == 404
    assert (await client.get(f"/tasks/{task['id']}/events", headers=bob.headers)).status_code == 404
    assert (await client.get(f"/tasks/{uuid.uuid4()}", headers=alice.headers)).status_code == 404


async def test_sse_replays_from_last_event_id(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    e1 = await emit(engine, tid, "node.completed", node="a")
    e2 = await emit(engine, tid, "node.completed", node="b")
    e3 = await emit(engine, tid, "task.finished", payload={"status": "completed"})
    r = await client.get(
        f"/tasks/{tid}/events", headers={**alice.headers, "Last-Event-ID": str(e1)}
    )
    assert r.status_code == 200
    got = _parse_sse(r.text)
    assert [e["id"] for e in got] == [e2, e3]
    assert got[-1]["type"] == "task.finished"
    assert f"id: {e2}" in r.text  # SSE id field set, so browsers resume correctly


async def test_sse_streams_live_events(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    first = await emit(engine, tid, "task.created")

    async def produce() -> None:
        await asyncio.sleep(0.3)
        await emit(engine, tid, "node.completed", node="step_one")
        await asyncio.sleep(0.1)
        await emit(engine, tid, "task.finished", payload={"status": "completed"})

    producer = asyncio.create_task(produce())
    r = await asyncio.wait_for(client.get(f"/tasks/{tid}/events", headers=alice.headers), 10)
    await producer
    types = [e["type"] for e in _parse_sse(r.text)]
    assert types == ["task.created", "node.completed", "task.finished"]
    assert _parse_sse(r.text)[0]["id"] == first


async def test_sse_rejects_bad_last_event_id(
    client: httpx.AsyncClient, engine: AsyncEngine, alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    r = await client.get(f"/tasks/{tid}/events", headers={**alice.headers, "Last-Event-ID": "x"})
    assert r.status_code == 400


async def test_dedupe_key_makes_emit_idempotent(engine: AsyncEngine, alice: SeededUser) -> None:
    """ADR-025: the same dedupe key twice -> one row, one NOTIFY; NULL keys never collide."""
    tid = await create_task(engine, alice)
    dsn = get_settings().libpq_dsn
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as listener:
        await listener.execute(f"LISTEN {CHANNEL}")
        key = node_key("plan", "started", "p1")
        first = await emit(engine, tid, "node.started", node="plan", dedupe_key=key)
        again = await emit(engine, tid, "node.started", node="plan", dedupe_key=key)
        free1 = await emit(engine, tid, "task.status", payload={"status": "running"})
        free2 = await emit(engine, tid, "task.status", payload={"status": "running"})
        # same key on another task is independent
        other = await create_task(engine, alice)
        elsewhere = await emit(engine, other, "node.started", node="plan", dedupe_key=key)

        notified: list[int] = []
        gen = listener.notifies(timeout=1.0)
        async for n in gen:
            msg = json.loads(n.payload)
            if msg["task_id"] == str(tid):
                notified.append(int(msg["id"]))

    assert first is not None and again is None
    assert free1 is not None and free2 is not None and free1 != free2
    assert elsewhere is not None
    assert notified == [first, free1, free2]
    async with engine.connect() as c:
        n = (
            await c.execute(
                text("SELECT count(*) FROM events WHERE task_id = :t AND dedupe_key = :k"),
                {"t": tid, "k": key},
            )
        ).scalar_one()
    assert n == 1


def test_node_key_skips_none_and_bounds_length() -> None:
    assert node_key("subtask", "started", "p1", None, "s1", "r0") == "subtask:started:p1:s1:r0"
    assert node_key("intake", "completed") == "intake:completed"
    with pytest.raises(ValueError):
        node_key("x", "y", "z" * 300)
