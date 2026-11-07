"""Approvals inbox (ADR-035) over HTTP: visibility, roles, validation, optimistic locking,
expiry (and its race with a human decision), and non-blocking side-effect reviews."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops import approvals
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def run_until_paused(
    engine: AsyncEngine, saver: Any, user: SeededUser, scenario: str
) -> uuid.UUID:
    tid = await create_task(
        engine, user, "Summarize pgvector", graph="main", config={"scenario": scenario}
    )
    await jobs.enqueue(engine, "run", thread_id=tid)
    await process(engine, saver)
    return tid


async def process(engine: AsyncEngine, saver: Any) -> None:
    s = get_settings()
    job = await jobs.claim(engine, "w-api", s.job_lease_seconds)
    assert job is not None
    await Worker(s, engine, saver, "w-api").process(job)


async def pending(client: httpx.AsyncClient, user: SeededUser, **params: Any) -> list[dict]:
    r = await client.get("/approvals", headers=user.headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def status(engine: AsyncEngine, tid: uuid.UUID) -> str:
    async with engine.connect() as c:
        return (
            await c.execute(text("SELECT status::text FROM tasks WHERE id=:t"), {"t": tid})
        ).scalar_one()


async def test_visibility_reviewer_sees_all_users_see_their_own(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    bob: SeededUser,
    reviewer: SeededUser,
) -> None:
    ta = await run_until_paused(engine, saver, alice, "revisions_exhausted")
    tb = await run_until_paused(engine, saver, bob, "reviewer_escalates")
    assert {a["task_id"] for a in await pending(client, reviewer)} == {str(ta), str(tb)}
    mine = await pending(client, alice)
    assert [a["task_id"] for a in mine] == [str(ta)]
    assert mine[0]["kind"] == "escalation" and mine[0]["key"] == "escalation:e1"
    assert mine[0]["payload"]["response_schema"]["title"] == "EscalationDecision"
    other = next(a for a in await pending(client, reviewer) if a["task_id"] == str(tb))
    assert (await client.get(f"/approvals/{other['id']}", headers=alice.headers)).status_code == 404
    assert (
        await client.get(f"/approvals/{other['id']}", headers=reviewer.headers)
    ).status_code == 200


async def test_deciding_needs_the_reviewer_role_and_a_valid_decision(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    await run_until_paused(engine, saver, alice, "revisions_exhausted")
    [a] = await pending(client, alice)
    url = f"/approvals/{a['id']}/decision"
    body = {"version": a["version"], "decision": {"action": "approve"}}
    assert (await client.post(url, json=body, headers=alice.headers)).status_code == 403
    for bad, needle in [
        ({"action": "maybe"}, "action"),
        ({"action": "edit"}, "edit needs `edits`"),
        ({"action": "edit", "edits": {"ghost": "x"}}, "not under escalation"),
        ({"action": "take_over"}, "take_over needs `deliverable`"),
        ({"action": "approve", "surprise": 1}, "Extra inputs"),
    ]:
        r = await client.post(
            url, json={"version": a["version"], "decision": bad}, headers=reviewer.headers
        )
        assert r.status_code == 422 and needle in r.text, (bad, r.text)
    assert (await pending(client, alice))[0]["status"] == "pending"  # nothing was written


async def test_api_decision_resumes_the_run(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    tid = await run_until_paused(engine, saver, alice, "revisions_exhausted")
    [a] = await pending(client, alice)
    r = await client.post(
        f"/approvals/{a['id']}/decision",
        json={
            "version": a["version"],
            "decision": {"action": "edit", "edits": {"draft": "Human draft."}},
        },
        headers=reviewer.headers,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["version"], out["decided_by"]) == ("edited", 2, str(reviewer.id))
    await process(engine, saver)  # the resume job the decision enqueued
    assert await status(engine, tid) == "completed"


async def test_two_concurrent_decisions_exactly_one_wins(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    tid = await run_until_paused(engine, saver, alice, "revisions_exhausted")
    [a] = await pending(client, alice)
    url = f"/approvals/{a['id']}/decision"
    r1, r2 = await asyncio.gather(
        client.post(
            url,
            json={"version": a["version"], "decision": {"action": "approve"}},
            headers=reviewer.headers,
        ),
        client.post(
            url,
            json={"version": a["version"], "decision": {"action": "reject"}},
            headers=reviewer.headers,
        ),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    async with engine.connect() as c:
        resumes = (
            await c.execute(
                text("SELECT count(*) FROM jobs WHERE thread_id=:t AND kind='resume'"), {"t": tid}
            )
        ).scalar_one()
    assert resumes == 1  # only the winner enqueued a resume


async def test_expiry_resolves_as_reject(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await run_until_paused(engine, saver, alice, "revisions_exhausted")
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 second' WHERE task_id=:t"),
            {"t": tid},
        )
    assert len(await approvals.sweep_expired(engine)) == 1
    assert await approvals.sweep_expired(engine) == []  # idempotent
    await process(engine, saver)
    assert await status(engine, tid) == "rejected"
    async with engine.connect() as c:
        st, decision = (
            await c.execute(
                text("SELECT status::text, decision FROM approvals WHERE task_id=:t"), {"t": tid}
            )
        ).one()
    assert st == "expired" and decision == {"action": "reject", "note": "approval expired"}


async def test_expiry_racing_a_human_decision_exactly_one_wins(
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    """Phase 4 change 3: the sweep and the API both go through the version-checked decide."""
    tid = await run_until_paused(engine, saver, alice, "revisions_exhausted")
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 second' WHERE task_id=:t"),
            {"t": tid},
        )
        aid, version = (
            await c.execute(text("SELECT id, version FROM approvals WHERE task_id=:t"), {"t": tid})
        ).one()

    async def human() -> bool:
        async with engine.begin() as c:
            won = await approvals.decide(
                c,
                aid,
                expected_version=version,
                decision={"action": "approve", "edits": {}, "deliverable": None, "note": None},
                status="approved",
                decided_by=reviewer.id,
            )
            if won is not None:
                await approvals.apply(c, won, {"action": "approve"}, "approved")
            return won is not None

    results = await asyncio.gather(human(), approvals.sweep_expired(engine))
    human_won, swept = results[0], results[1]
    assert human_won != bool(swept)  # exactly one transition
    async with engine.connect() as c:
        st = (
            await c.execute(text("SELECT status::text FROM approvals WHERE id=:a"), {"a": aid})
        ).scalar_one()
        resumes = (
            await c.execute(
                text("SELECT count(*) FROM jobs WHERE thread_id=:t AND kind='resume'"), {"t": tid}
            )
        ).scalar_one()
    assert st == ("approved" if human_won else "expired") and resumes == 1


async def test_side_effect_review_only_updates_records(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    alice: SeededUser,
    reviewer: SeededUser,
) -> None:
    """Phase 4 change 1: a non-blocking inbox item; resolving it never re-executes anything."""
    tid = await create_task(engine, alice, "x", graph="main")
    key = uuid.uuid4().hex
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO side_effects (idempotency_key, task_id, action_hash, status, tool, args) "
                "VALUES (:k, :t, 'h', 'in_progress', 'send_email', '{}'::jsonb)"
            ),
            {"k": key, "t": tid},
        )
        await approvals.record_pending(
            c,
            tid,
            kind="side_effect_review",
            key=f"side_effect:{key}",
            payload={"idempotency_key": key, "tool": "send_email"},
            settings=get_settings(),
            blocking=False,
        )
    [a] = await pending(client, reviewer, task_id=str(tid))
    assert a["blocking"] is False
    r = await client.post(
        f"/approvals/{a['id']}/decision",
        json={
            "version": a["version"],
            "decision": {"action": "confirmed_done", "note": "saw it in the outbox"},
        },
        headers=reviewer.headers,
    )
    assert r.status_code == 200, r.text
    async with engine.connect() as c:
        se_status, err = (
            await c.execute(
                text("SELECT status::text, error FROM side_effects WHERE idempotency_key=:k"),
                {"k": key},
            )
        ).one()
        jobs_n = (
            await c.execute(text("SELECT count(*) FROM jobs WHERE thread_id=:t"), {"t": tid})
        ).scalar_one()
    assert se_status == "done" and "confirmed_done (saw it in the outbox)" in err
    assert jobs_n == 0  # no resume, no re-execution
    # not swept by expiry either: it's non-blocking
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE approvals SET status='pending', expires_at = now() - interval '1 day' WHERE task_id=:t"
            ),
            {"t": tid},
        )
    assert await approvals.sweep_expired(engine) == []
