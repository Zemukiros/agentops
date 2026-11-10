"""Durability probe (Phase 1 acceptance): a run checkpointed by worker process A continues
on worker process B after A is SIGKILLed mid-node, and two workers never advance the same
thread at once. Synchronization uses marker files, not sleeps."""

import os
import signal
import uuid
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.graph import build_demo_graph
from agentops.core.testhooks import TEST_HOOK_NODES_ENV
from agentops.db.checkpointer import open_checkpointer
from agentops.queue import jobs
from agentops.settings import get_settings
from tests.integration.conftest import (
    SeededUser,
    create_task,
    finish,
    graph_state_values,
    pending_approvals,
    wait_for,
)

pytestmark = pytest.mark.integration


async def _events(engine: AsyncEngine, tid: uuid.UUID) -> list[tuple]:
    async with engine.connect() as c:
        rows = await c.execute(
            text("SELECT type, node, payload FROM events WHERE task_id = :t ORDER BY id"),
            {"t": tid},
        )
        return [tuple(r) for r in rows]


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as c:
        return (await c.execute(text(sql), params)).scalar_one()


async def test_run_resumes_from_checkpoint_on_another_process_after_crash(
    engine: AsyncEngine, alice: SeededUser, hook_dir: Path, spawn
) -> None:
    tid = await create_task(engine, alice)
    job_id = await jobs.enqueue(engine, "run", thread_id=tid)

    # Worker A runs step_one, then blocks inside step_two.
    a = spawn("worker-A", "--once")
    wait_for(hook_dir / f"{tid}.step_two.started", a)

    # The checkpoint after step_one is durable and points at step_two.
    async with open_checkpointer(get_settings().libpq_dsn, max_size=1) as saver:
        cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
        state = await build_demo_graph(saver).aget_state(cfg)
    assert state.next == ("step_two",)
    assert state.values["log"] == [f"step_one:{a.pid}"]

    # Crash A hard: no cleanup, no lease release. Postgres drops its advisory lock.
    os.kill(a.pid, signal.SIGKILL)
    a.wait(10)

    # Worker B can't take the job until A's lease expires, then resumes from the checkpoint.
    (hook_dir / f"{tid}.release").write_text("go")
    b = spawn("worker-B", "--once", "--exit-after", "30")
    finish(b, timeout=40)

    assert (
        await _scalar(engine, "SELECT status::text FROM tasks WHERE id = :t", t=tid) == "completed"
    )
    job = await _scalar(engine, "SELECT attempts FROM jobs WHERE id = :j", j=job_id)
    assert job == 2
    assert await _scalar(engine, "SELECT status::text FROM jobs WHERE id = :j", j=job_id) == "done"

    ev = await _events(engine, tid)
    completed = [(n, p["pid"]) for t, n, p in ev if t == "node.completed"]
    # step_one ran exactly once (on A); step_two completed exactly once (on B)
    assert completed == [("step_one", a.pid), ("step_two", b.pid)]
    # step_two was interrupted on A and re-run from its first line on B (the completion
    # above came from B). Its "started" event is idempotent (ADR-025): the re-run produced
    # the same dedupe key, so the stream holds exactly one, the original from A.
    started_two = [p["pid"] for t, n, p in ev if t == "node.started" and n == "step_two"]
    assert started_two == [a.pid]
    segments = [p for t, _, p in ev if t == "run.segment_started"]
    assert [s["worker"] for s in segments] == ["worker-A", "worker-B"]
    assert [s["resumed_from_checkpoint"] for s in segments] == [False, True]
    assert ev[-1][0] == "task.finished" and ev[-1][2]["status"] == "completed"

    # Checkpoint history: one input checkpoint, so the run continued rather than restarted.
    async with open_checkpointer(get_settings().libpq_dsn, max_size=1) as saver:
        graph = build_demo_graph(saver)
        final = await graph.aget_state(cfg)
        history = [h async for h in graph.aget_state_history(cfg)]
    assert final.next == ()
    assert final.values["log"] == [f"step_one:{a.pid}", f"step_two:{b.pid}"]
    assert [(h.metadata or {}).get("source") for h in history].count("input") == 1


async def test_two_workers_racing_on_one_thread_only_one_advances(
    engine: AsyncEngine, alice: SeededUser, hook_dir: Path, spawn
) -> None:
    """A duplicate job for the same thread (e.g. redelivery racing a resume) must not run
    concurrently: the second worker sees the advisory lock and gives the job back."""
    tid = await create_task(engine, alice)
    j1 = await jobs.enqueue(engine, "run", thread_id=tid)

    a = spawn("worker-A", "--once")
    wait_for(hook_dir / f"{tid}.step_two.started", a)

    j2 = await jobs.enqueue(engine, "run", thread_id=tid)  # duplicate delivery
    b = spawn("worker-B", "--once", "--exit-after", "20")
    out_b = finish(b)
    assert "locked elsewhere" in out_b

    row = await _scalar(
        engine, "SELECT status::text || ':' || attempts FROM jobs WHERE id = :j", j=j2
    )
    assert row == "queued:0"  # released without consuming an attempt
    ev = await _events(engine, tid)
    assert [p["worker"] for t, _, p in ev if t == "run.segment_started"] == ["worker-A"]

    # Let A finish; then a third worker drains the duplicate as a no-op (task already terminal).
    (hook_dir / f"{tid}.release").write_text("go")
    finish(a)
    c = spawn("worker-C", "--once", "--exit-after", "20")
    finish(c)

    ev = await _events(engine, tid)
    assert [n for t, n, _ in ev if t == "node.completed"] == ["step_one", "step_two"]
    assert [p["worker"] for t, _, p in ev if t == "run.segment_started"] == ["worker-A"]
    statuses = await _scalar(
        engine, "SELECT string_agg(id || ':' || status::text, ',' ORDER BY id) FROM jobs"
    )
    assert statuses == f"{j1}:done,{j2}:done"


async def test_main_graph_crash_inside_specialist_resumes_on_another_worker(
    engine: AsyncEngine, alice: SeededUser, hook_dir: Path, spawn
) -> None:
    """Phase 2: the real graph survives a SIGKILL inside a parallel branch (the subtask_worker
    subgraph). The plan is not redone, node events are not duplicated (ADR-025), and the run
    ends in exactly one terminal state."""
    tid = await create_task(
        engine, alice, "Summarize pgvector", graph="main", config={"scenario": "revise_once"}
    )
    await jobs.enqueue(engine, "run", thread_id=tid)
    hooked = {TEST_HOOK_NODES_ENV: "specialist"}

    a = spawn("worker-A", "--once", env_extra=hooked)
    wait_for(hook_dir / f"{tid}.specialist.started", a)
    os.kill(a.pid, signal.SIGKILL)
    a.wait(10)

    (hook_dir / f"{tid}.release").write_text("go")
    b = spawn("worker-B", "--once", "--exit-after", "30", env_extra=hooked)
    finish(b, timeout=40)

    assert await _scalar(engine, "SELECT status::text FROM tasks WHERE id = :t", t=tid) == (
        "completed"
    )
    ev = await _events(engine, tid)
    # one started event per specialist execution (revision 0 and 1), despite the re-run of r0
    started = [p["subtask_id"] for t, n, p in ev if t == "node.started" and n == "specialist"]
    assert started == ["draft", "draft"]
    async with engine.connect() as c:
        purposes = [
            r[0]
            for r in await c.execute(
                text(
                    "SELECT split_part(purpose, '/', 1) FROM llm_calls "
                    "WHERE task_id = :t ORDER BY id"
                ),
                {"t": tid},
            )
        ]
    # A died before its first draft call; B did not re-plan
    assert purposes == [
        "plan",
        "act",
        "review",
        "act",
        "review",
        "synthesize",
        "final_review",
        "consolidate",
    ]
    segments = [p for t, _, p in ev if t == "run.segment_started"]
    assert [s["worker"] for s in segments] == ["worker-A", "worker-B"]
    assert [s["resumed_from_checkpoint"] for s in segments] == [False, True]
    assert [t for t, _, _ in ev].count("task.finished") == 1


async def test_rerun_after_crash_cannot_duplicate_a_proposal(
    engine: AsyncEngine, alice: SeededUser, hook_dir: Path, spawn
) -> None:
    """Phase 3 change 2: A is SIGKILLed in the tools node after the registry recorded a
    proposal but before the node returned; B re-runs the node. Exactly one proposal survives.
    Note what this proves: a killed node's writes are never committed (LangGraph only persists
    a node's update when it returns), so the re-run is the only write. Repeated proposals that
    ARE each committed (across revisions, or parallel subtasks) rely on the hash-keyed reducer;
    test_proposals.py::test_same_action_proposed_in_two_revisions_is_kept_once covers that."""
    tid = await create_task(
        engine, alice, "Summarize and share", graph="main", config={"scenario": "proposes_email"}
    )
    await jobs.enqueue(engine, "run", thread_id=tid)
    hooked = {TEST_HOOK_NODES_ENV: "tools"}

    a = spawn("worker-A", "--once", env_extra=hooked)
    wait_for(hook_dir / f"{tid}.tools.started", a)
    os.kill(a.pid, signal.SIGKILL)
    a.wait(10)
    (hook_dir / f"{tid}.release").write_text("go")
    b = spawn("worker-B", "--once", "--exit-after", "30", env_extra=hooked)
    finish(b, timeout=40)

    async with engine.connect() as c:
        status, _result = (
            await c.execute(text("SELECT status::text, result FROM tasks WHERE id=:t"), {"t": tid})
        ).one()
        attempts = (
            await c.execute(
                text(
                    "SELECT count(*) FROM tool_calls WHERE task_id=:t AND tool='send_email' "
                    "AND status='proposed'"
                ),
                {"t": tid},
            )
        ).scalar_one()
    assert status == "awaiting_approval"  # Phase 4: paused at action_gate
    assert attempts == 2  # both executions of the node are logged: nothing hidden
    [proposal] = (await graph_state_values(tid))["proposed_actions"].values()  # state holds one
    assert proposal.tool == "send_email" and proposal.args["to"] == "team@example.com"
    assert await pending_approvals(engine, tid) == [("approve_action", f"action:{proposal.hash}")]


async def test_a_steps_checkpoint_is_committed_before_the_next_step_starts(
    engine: AsyncEngine, alice: SeededUser, hook_dir: Path, spawn
) -> None:
    """ADR-051. LangGraph's default durability ("async") writes a step's checkpoint while the
    next step already runs, so a worker killed early in step_two could lose step_one's
    checkpoint. This failed in CI on a slow runner. Slow checkpoint writes (1.5 s) make the
    race certain: with durability="sync" the checkpoint is there the moment step_two starts."""
    tid = await create_task(engine, alice)
    await jobs.enqueue(engine, "run", thread_id=tid)
    a = spawn("worker-slow", "--once", env_extra={"AGENTOPS_TEST_CHECKPOINT_DELAY_S": "1.5"})
    wait_for(hook_dir / f"{tid}.step_two.started", a)
    async with open_checkpointer(get_settings().libpq_dsn, max_size=1) as saver:
        cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
        state = await build_demo_graph(saver).aget_state(cfg)
    assert state.next == ("step_two",)
    assert state.values["log"] == [f"step_one:{a.pid}"]
    os.kill(a.pid, signal.SIGKILL)
    a.wait(10)
