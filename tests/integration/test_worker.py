"""Worker failure paths (ADR-022): a lost lease cancels the run; an exhausted job whose
worker died is reaped and its task ends `failed`."""

import asyncio
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.graph import TEST_HOOK_ENV, build_demo_graph
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list[tuple]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(text(sql), params)]


async def _wait_for_file(path: Path, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(0.02)


async def test_lost_lease_cancels_the_run(
    engine: AsyncEngine, alice: SeededUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_HOOK_ENV, str(tmp_path))
    settings = get_settings().model_copy(
        update={"job_lease_seconds": 30.0, "job_heartbeat_seconds": 0.2}
    )
    tid = await create_task(engine, alice)
    job_id = await jobs.enqueue(engine, "run", thread_id=tid)

    async with open_checkpointer(settings.libpq_dsn, max_size=2) as saver:
        worker = Worker(settings, engine, saver, "worker-A")
        job = await jobs.claim(engine, "worker-A", settings.job_lease_seconds)
        assert job is not None and job.id == job_id
        run = asyncio.create_task(worker.process(job))

        # The graph is blocked inside step_two. Another worker "steals" the lease.
        await _wait_for_file(tmp_path / f"{tid}.step_two.started")
        async with engine.begin() as c:
            await c.execute(
                text("UPDATE jobs SET lease_owner = 'thief' WHERE id = :j"), {"j": job_id}
            )

        # The next heartbeat sees it no longer owns the job and cancels the run. The
        # release file is never written, so the only way `process` returns is that
        # cancellation. asyncio.wait does not cancel `run` on timeout (a timeout context
        # would, and would mask a broken heartbeat).
        done, _ = await asyncio.wait({run}, timeout=5)
        assert run in done, "run was not cancelled after the lease was lost"
        assert run.exception() is None  # lease loss is handled, not raised

        cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
        state = await build_demo_graph(saver).aget_state(cfg)

    assert not (tmp_path / f"{tid}.release").exists()
    assert state.next == ("step_two",)  # checkpoint did not advance past the cancelled node
    ev = await _rows(engine, "SELECT type, node FROM events WHERE task_id = :t ORDER BY id", t=tid)
    assert ("node.completed", "step_two") not in ev
    assert all(t != "task.finished" for t, _ in ev)
    # The job still belongs to the new owner: the old worker neither completed nor failed it.
    assert await _rows(
        engine, "SELECT status::text, lease_owner FROM jobs WHERE id = :j", j=job_id
    ) == [("running", "thief")]
    assert await _rows(engine, "SELECT status::text FROM tasks WHERE id = :t", t=tid) == [
        ("running",)
    ]


async def test_external_cancel_is_not_mistaken_for_lease_loss(
    engine: AsyncEngine, alice: SeededUser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown/caller cancellation must propagate, not be logged as a lease loss."""
    monkeypatch.setenv(TEST_HOOK_ENV, str(tmp_path))
    settings = get_settings().model_copy(update={"job_heartbeat_seconds": 0.2})
    tid = await create_task(engine, alice)
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(settings.libpq_dsn, max_size=2) as saver:
        worker = Worker(settings, engine, saver, "worker-A")
        job = await jobs.claim(engine, "worker-A", settings.job_lease_seconds)
        assert job is not None
        run = asyncio.create_task(worker.process(job))
        await _wait_for_file(tmp_path / f"{tid}.step_two.started")
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run


async def test_reaper_fails_exhausted_job_and_its_task(
    engine: AsyncEngine, alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    other = await create_task(engine, alice)
    async with engine.begin() as c:
        # Died on its last attempt: lease expired, attempts == max_attempts.
        dead = (
            await c.execute(
                text(
                    "INSERT INTO jobs (kind, thread_id, status, attempts, max_attempts, "
                    "lease_owner, lease_expires_at) VALUES ('run', :t, 'running', 3, 3, "
                    "'dead-worker', now() - interval '1 minute') RETURNING id"
                ),
                {"t": tid},
            )
        ).scalar_one()
        # Expired lease but attempts left: must NOT be reaped (it is reclaimable instead).
        await c.execute(
            text(
                "INSERT INTO jobs (kind, thread_id, status, attempts, max_attempts, "
                "lease_owner, lease_expires_at) VALUES ('run', :t, 'running', 1, 3, "
                "'dead-worker', now() - interval '1 minute')"
            ),
            {"t": other},
        )
        await c.execute(
            text("UPDATE tasks SET status = 'running' WHERE id IN (:a, :b)"), {"a": tid, "b": other}
        )

    async with open_checkpointer(get_settings().libpq_dsn, max_size=1) as saver:
        worker = Worker(get_settings(), engine, saver, "reaper")
        assert await worker.reap_once() == [dead]
        assert await worker.reap_once() == []  # idempotent

    assert await _rows(engine, "SELECT status::text FROM jobs WHERE id = :j", j=dead) == [
        ("failed",)
    ]
    assert await _rows(engine, "SELECT status::text, error FROM tasks WHERE id = :t", t=tid) == [
        ("failed", "worker lost")
    ]
    finished = await _rows(
        engine,
        "SELECT payload->>'status' FROM events WHERE task_id = :t AND type = 'task.finished'",
        t=tid,
    )
    assert finished == [("failed",)]
    assert await _rows(engine, "SELECT status::text FROM tasks WHERE id = :t", t=other) == [
        ("running",)
    ]
