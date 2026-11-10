"""Phase 6 hardening: pagination (approvals, checkpoints), API rate limits, and a guard that
every endpoint requires authentication."""

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.api.main import create_app
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from tests.integration.conftest import SeededUser

pytestmark = pytest.mark.integration

PUBLIC = {("GET", "/health")}


def api_routes(app: Any) -> list[APIRoute]:
    """Every APIRoute, including those inside included routers (FastAPI wraps them in an
    _IncludedRouter that a flat scan of app.routes does not see)."""

    def walk(routes: list[Any]) -> list[APIRoute]:
        out: list[APIRoute] = []
        for r in routes:
            if isinstance(r, APIRoute):
                out.append(r)
            elif hasattr(r, "original_router"):
                out += walk(r.original_router.routes)
        return out

    found = walk(app.routes)
    # never vacuous: the walk must see every operation the OpenAPI schema lists
    ops = sum(len(v) for v in app.openapi()["paths"].values())
    assert sum(len(r.methods or ()) for r in found) == ops > 20
    return found


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def process(engine: AsyncEngine, saver: Any) -> None:
    s = get_settings()
    while (job := await jobs.claim(engine, "w-hard", s.job_lease_seconds)) is not None:
        await Worker(s, engine, saver, "w-hard").process(job)


def test_every_route_requires_authentication(app: Any) -> None:
    """Static: each non-public route depends on get_current_user (directly or through a role /
    rate-limit / ticket dependency). A new unauthenticated route fails this test."""
    from agentops.api.auth import get_current_user  # noqa: PLC0415
    from agentops.api.routes.tasks import stream_user  # noqa: PLC0415

    def deps(dep: Any) -> set[Any]:
        out = {dep.call}
        for d in dep.dependencies:
            out |= deps(d)
        return out

    missing = []
    for r in api_routes(app):
        for m in r.methods or ():
            if (m, r.path) in PUBLIC:
                continue
            calls = deps(r.dependant)
            if get_current_user not in calls and stream_user not in calls:
                missing.append(f"{m} {r.path}")
    assert missing == []


async def test_every_route_rejects_anonymous_requests(app: Any, client: httpx.AsyncClient) -> None:
    """Dynamic: call each route without credentials (path ids filled with random UUIDs)."""
    checked = 0
    for r in api_routes(app):
        for m in r.methods or ():
            if (m, r.path) in PUBLIC or m == "HEAD":
                continue
            path = re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), r.path)
            resp = await client.request(m, path, json={} if m == "POST" else None)
            assert resp.status_code == 401, (m, r.path, resp.status_code)
            checked += 1
    assert checked >= 20


async def test_approvals_paginate_through_a_header(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    for i in range(5):
        r = await client.post(
            "/tasks",
            headers=alice.headers,
            json={"request": f"Summarize {i}", "scenario": "proposes_email"},
        )
        assert r.status_code == 201
    await process(engine, saver)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/approvals", headers=reviewer.headers, params=params)
        assert r.status_code == 200
        seen += [a["id"] for a in r.json()]
        pages += 1
        cursor = r.headers.get("X-Next-Cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5 and pages == 3
    assert (
        await client.get("/approvals", headers=reviewer.headers, params={"cursor": "x"})
    ).status_code == 400


async def test_checkpoints_paginate(
    client: httpx.AsyncClient, engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = (
        await client.post("/tasks", headers=alice.headers, json={"request": "Summarize"})
    ).json()["id"]
    await process(engine, saver)
    full = (await client.get(f"/tasks/{tid}/checkpoints", headers=alice.headers)).json()
    assert full["next_cursor"] is None
    ids: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 4}
        if cursor:
            params["cursor"] = cursor
        page = (
            await client.get(f"/tasks/{tid}/checkpoints", headers=alice.headers, params=params)
        ).json()
        ids += [c["checkpoint_id"] for c in page["checkpoints"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert ids == [c["checkpoint_id"] for c in full["checkpoints"]]
    bad = await client.get(
        f"/tasks/{tid}/checkpoints", headers=alice.headers, params={"cursor": "nope"}
    )
    assert bad.status_code == 400


async def test_rate_limit_returns_429_with_retry_after(
    test_db: str, alice: SeededUser, bob: SeededUser
) -> None:
    settings = get_settings().model_copy(
        update={"api_rate_limits": {"create_task": 2, "stream_ticket": 0}}
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        codes = [
            (await c.post("/tasks", headers=alice.headers, json={"request": "x"})).status_code
            for _ in range(3)
        ]
        assert codes == [201, 201, 429]
        r = await c.post("/tasks", headers=alice.headers, json={"request": "x"})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        # per user: bob is unaffected
        assert (
            await c.post("/tasks", headers=bob.headers, json={"request": "x"})
        ).status_code == 201
