"""Phase 6 demo hygiene: tagged test/eval runs are hidden, dev-only task deletion purges the
whole tree, reset-demo is dev-only, scenarios carry descriptions and default to ui_demo."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.api.main import create_app
from agentops.db import reset_demo
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from tests.integration.conftest import SeededUser

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def process(engine: AsyncEngine, saver: Any) -> None:
    s = get_settings()
    job = await jobs.claim(engine, "w-hyg", s.job_lease_seconds)
    assert job is not None
    await Worker(s, engine, saver, "w-hyg").process(job)


async def submit(client: httpx.AsyncClient, user: SeededUser, **body: Any) -> str:
    r = await client.post("/tasks", headers=user.headers, json={"request": "Summarize", **body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def count(engine: AsyncEngine, sql: str, **p: Any) -> int:
    async with engine.connect() as c:
        return int((await c.execute(text(sql), p)).scalar_one())


async def test_tagged_runs_are_hidden_unless_asked_for(
    client: httpx.AsyncClient, alice: SeededUser
) -> None:
    real = await submit(client, alice)
    e2e = await submit(client, alice, tags=["e2e"])
    other = await submit(client, alice, tags=["demo"])
    shown = [t["id"] for t in (await client.get("/tasks", headers=alice.headers)).json()["items"]]
    assert set(shown) == {real, other}
    every = (
        await client.get("/tasks", headers=alice.headers, params={"include_hidden": "true"})
    ).json()["items"]
    assert {t["id"] for t in every} == {real, e2e, other}
    bad = await client.post(
        "/tasks", headers=alice.headers, json={"request": "x", "tags": ["Not Valid!"]}
    )
    assert bad.status_code == 422


async def test_delete_purges_the_whole_tree_and_is_guarded(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    bob: SeededUser,
) -> None:
    root = await submit(client, alice)
    await process(engine, saver)
    cps = (await client.get(f"/tasks/{root}/checkpoints", headers=alice.headers)).json()
    cp = next(c for c in cps["checkpoints"] if c["forkable"] and c["next"] == ["final_review"])
    branch = (
        await client.post(
            f"/tasks/{root}/branches",
            headers=alice.headers,
            json={"checkpoint_id": cp["checkpoint_id"]},
        )
    ).json()["id"]
    await process(engine, saver)
    running = await submit(client, alice)  # queued: not deletable yet
    assert await count(engine, "SELECT count(*) FROM memories WHERE source_task_id=:t", t=root) > 0

    assert (await client.delete(f"/tasks/{root}", headers=bob.headers)).status_code == 404
    assert (await client.delete(f"/tasks/{running}", headers=alice.headers)).status_code == 409
    assert (await client.delete(f"/tasks/{root}", headers=alice.headers)).status_code == 204

    for tid in (root, branch):
        assert await count(engine, "SELECT count(*) FROM tasks WHERE id=:t", t=tid) == 0
        assert await count(engine, "SELECT count(*) FROM events WHERE task_id=:t", t=tid) == 0
        assert (
            await count(engine, "SELECT count(*) FROM checkpoints WHERE thread_id=:t", t=str(tid))
            == 0
        )
    assert await count(engine, "SELECT count(*) FROM memories WHERE source_task_id IS NULL") == 0
    assert await count(engine, "SELECT count(*) FROM tasks WHERE id=:t", t=running) == 1


async def test_delete_and_reset_are_refused_in_production(alice: SeededUser, test_db: str) -> None:
    prod = get_settings().model_copy(update={"env": "prod"})
    app = create_app(prod)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.delete(f"/tasks/{uuid.uuid4()}", headers=alice.headers)
    assert r.status_code == 403 and "production" in r.json()["detail"]
    with pytest.raises(SystemExit, match="disabled when ENV=prod"):
        reset_demo.refuse_in_prod(prod)


async def test_reset_clears_runs_but_keeps_users(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    tmp_path: Path,
) -> None:
    await submit(client, alice)
    await process(engine, saver)
    (tmp_path / "ws" / "some-task").mkdir(parents=True)
    settings = get_settings().model_copy(
        update={"workspace_root": tmp_path / "ws", "mailpit_api_url": "http://127.0.0.1:9"}
    )
    out = await reset_demo.reset(settings)
    assert out["tasks"] == 1 and out["memories"] >= 1
    for t in ("tasks", "memories", "events", "checkpoints", "approvals"):
        assert await count(engine, f"SELECT count(*) FROM {t}") == 0, t
    assert await count(engine, "SELECT count(*) FROM users") >= 1
    assert list((tmp_path / "ws").iterdir()) == []
    assert (await client.get("/me", headers=alice.headers)).status_code == 200  # token works


async def test_scenarios_have_descriptions_and_default_to_ui_demo(
    client: httpx.AsyncClient, alice: SeededUser
) -> None:
    body = (await client.get("/tasks/scenarios", headers=alice.headers)).json()
    assert body["default"] == "ui_demo"
    names = [s["name"] for s in body["scenarios"]]
    assert names[0] == "ui_demo"
    assert all(s["description"] for s in body["scenarios"])
