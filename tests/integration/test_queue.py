import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.queue import jobs
from agentops.queue.locks import thread_lock
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


async def _job(engine: AsyncEngine, job_id: int) -> dict:
    async with engine.connect() as c:
        r = (await c.execute(text("SELECT * FROM jobs WHERE id = :i"), {"i": job_id})).first()
    assert r is not None
    return dict(r._mapping)


async def test_enqueue_claim_complete(engine: AsyncEngine, alice: SeededUser) -> None:
    tid = await create_task(engine, alice)
    jid = await jobs.enqueue(engine, "run", thread_id=tid, payload={"x": 1})
    job = await jobs.claim(engine, "w1", 30)
    assert job is not None and job.id == jid and job.thread_id == tid
    assert job.payload == {"x": 1} and job.attempts == 1
    assert await jobs.claim(engine, "w2", 30) is None  # leased, not claimable
    assert await jobs.complete(engine, jid, "w1")
    row = await _job(engine, jid)
    assert row["status"] == "done" and row["finished_at"] is not None


async def test_concurrent_claimers_never_share_a_job(engine: AsyncEngine) -> None:
    n_jobs, n_workers = 40, 8
    ids = {await jobs.enqueue(engine, "maintenance") for _ in range(n_jobs)}
    claimed: list[tuple[str, int]] = []

    async def claimer(name: str) -> None:
        while (j := await jobs.claim(engine, name, 30)) is not None:
            claimed.append((name, j.id))

    await asyncio.gather(*(claimer(f"w{i}") for i in range(n_workers)))
    got = [jid for _, jid in claimed]
    assert sorted(got) == sorted(ids)  # every job exactly once
    assert len({w for w, _ in claimed}) > 1  # actually concurrent


async def test_expired_lease_is_reclaimed_and_old_owner_fenced(engine: AsyncEngine) -> None:
    jid = await jobs.enqueue(engine, "maintenance")
    first = await jobs.claim(engine, "dead-worker", 0.2)
    assert first is not None
    await asyncio.sleep(0.4)
    second = await jobs.claim(engine, "w2", 30)
    assert second is not None and second.id == jid and second.attempts == 2
    # the original owner can no longer touch the job
    assert not await jobs.heartbeat(engine, jid, "dead-worker", 30)
    assert not await jobs.complete(engine, jid, "dead-worker")
    assert await jobs.fail(engine, jid, "dead-worker", "x") is None
    assert await jobs.heartbeat(engine, jid, "w2", 30)
    assert await jobs.complete(engine, jid, "w2")


async def test_heartbeat_extends_lease(engine: AsyncEngine) -> None:
    jid = await jobs.enqueue(engine, "maintenance")
    await jobs.claim(engine, "w1", 0.5)
    for _ in range(3):
        await asyncio.sleep(0.25)
        assert await jobs.heartbeat(engine, jid, "w1", 0.5)
    assert await jobs.claim(engine, "w2", 30) is None


async def test_fail_retries_with_backoff_then_fails(engine: AsyncEngine) -> None:
    jid = await jobs.enqueue(engine, "maintenance", max_attempts=2)
    await jobs.claim(engine, "w1", 30)
    assert await jobs.fail(engine, jid, "w1", "boom") == "queued"
    assert await jobs.claim(engine, "w1", 30) is None  # backoff: run_after in the future
    async with engine.begin() as c:
        await c.execute(text("UPDATE jobs SET run_after = now() WHERE id = :i"), {"i": jid})
    j = await jobs.claim(engine, "w1", 30)
    assert j is not None and j.attempts == 2
    assert await jobs.fail(engine, jid, "w1", "boom again") == "failed"
    row = await _job(engine, jid)
    assert row["status"] == "failed" and row["last_error"] == "boom again"


async def test_release_does_not_consume_attempt(engine: AsyncEngine) -> None:
    jid = await jobs.enqueue(engine, "maintenance")
    await jobs.claim(engine, "w1", 30)
    assert await jobs.release(engine, jid, "w1", 0)
    row = await _job(engine, jid)
    assert row["status"] == "queued" and row["attempts"] == 0


async def test_reap_exhausted_after_final_lease_expiry(engine: AsyncEngine) -> None:
    jid = await jobs.enqueue(engine, "maintenance", max_attempts=1)
    await jobs.claim(engine, "dead", 0.1)
    await asyncio.sleep(0.3)
    assert await jobs.claim(engine, "w2", 30) is None  # no attempts left
    reaped = await jobs.reap_exhausted(engine)
    assert [r[0] for r in reaped] == [jid]
    assert (await _job(engine, jid))["status"] == "failed"


async def test_advisory_lock_is_exclusive_per_thread(engine: AsyncEngine) -> None:
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    async with thread_lock(engine, t1) as a:
        assert a
        async with thread_lock(engine, t1) as b:
            assert not b  # same thread, other session
        async with thread_lock(engine, t2) as c:
            assert c  # different thread is independent
    async with thread_lock(engine, t1) as again:
        assert again  # released on exit
