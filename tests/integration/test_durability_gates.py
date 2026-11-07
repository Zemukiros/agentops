"""Durability matrix (Phase 4 item 6): a worker is SIGKILLed at each gate (before its
interrupt is persisted, and after a resume value arrived) and inside execute_actions; a
DIFFERENT worker resumes. No duplicate side effects, no duplicate events, one terminal state."""

import os
import signal
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.nodes.actions import idempotency_key
from agentops.core.testhooks import TEST_HOOK_NODES_ENV
from agentops.queue import jobs
from tests.integration.conftest import (
    SeededUser,
    create_task,
    decide,
    finish,
    graph_state_values,
    wait_for,
)

pytestmark = pytest.mark.integration


def kill_in(spawn, hook_dir: Path, tid: uuid.UUID, node: str, worker: str) -> None:
    """Run one job on a fresh worker that blocks in `node`, then SIGKILL it there."""
    (hook_dir / f"{tid}.release").unlink(missing_ok=True)
    p = spawn(worker, "--once", env_extra={TEST_HOOK_NODES_ENV: node})
    wait_for(hook_dir / f"{tid}.{node}.started", p)
    os.kill(p.pid, signal.SIGKILL)
    p.wait(10)


def resume_on(spawn, hook_dir: Path, tid: uuid.UUID, worker: str) -> None:
    """A different worker (no hooks) takes over once the dead one's lease expires."""
    (hook_dir / f"{tid}.release").write_text("go")
    finish(spawn(worker, "--once", "--exit-after", "30"), timeout=40)


async def scalar(engine: AsyncEngine, sql: str, **p: object) -> object:
    async with engine.connect() as c:
        return (await c.execute(text(sql), p)).scalar_one()


async def status(engine: AsyncEngine, tid: uuid.UUID) -> str:
    return str(await scalar(engine, "SELECT status::text FROM tasks WHERE id=:t", t=tid))


async def assert_clean(engine: AsyncEngine, tid: uuid.UUID, final: str, once: list[str]) -> None:
    assert await status(engine, tid) == final
    async with engine.connect() as c:
        counts: dict[str, int] = dict(
            (
                await c.execute(
                    text("SELECT type, count(*) FROM events WHERE task_id=:t GROUP BY type"),
                    {"t": tid},
                )
            ).all()
        )
        approvals_n = (
            await c.execute(text("SELECT count(*) FROM approvals WHERE task_id=:t"), {"t": tid})
        ).scalar_one()
        keys_n = (
            await c.execute(
                text("SELECT count(DISTINCT key) FROM approvals WHERE task_id=:t"), {"t": tid}
            )
        ).scalar_one()
    assert counts.get("task.finished") == 1
    assert (
        approvals_n == keys_n == counts.get("approval.requested", 0)
    )  # one row per decision point
    for t in once:
        assert counts.get(t) == 1, (t, counts.get(t))


async def start(engine: AsyncEngine, user: SeededUser, scenario: str) -> uuid.UUID:
    tid = await create_task(
        engine, user, "Summarize and share", graph="main", config={"scenario": scenario}
    )
    await jobs.enqueue(engine, "run", thread_id=tid)
    return tid


async def mails_with_key(key: str) -> int:
    async with httpx.AsyncClient() as c:
        r = await c.get("http://localhost:8025/api/v1/messages", params={"limit": 1000})
    return sum(1 for m in r.json()["messages"] if m.get("MessageID") == f"{key}@agentops")


async def email_key(tid: uuid.UUID) -> str:
    [p] = (await graph_state_values(tid))["proposed_actions"].values()
    return idempotency_key(str(tid), p.hash, p.args)


async def test_kill_at_plan_gate_before_and_after_the_decision(
    engine: AsyncEngine,
    alice: SeededUser,
    hook_dir: Path,
    spawn,
) -> None:
    tid = await start(engine, alice, "low_confidence_plan")
    kill_in(spawn, hook_dir, tid, "plan_gate", "w-A")
    resume_on(spawn, hook_dir, tid, "w-B")
    assert await status(engine, tid) == "awaiting_approval"
    await decide(engine, tid, "approve_plan", {"action": "approve"})
    kill_in(spawn, hook_dir, tid, "plan_gate_resumed", "w-C")
    resume_on(spawn, hook_dir, tid, "w-D")
    await assert_clean(
        engine, tid, "completed", ["plan.confidence", "plan_gate.resolved", "memory.consolidated"]
    )


async def test_kill_at_action_gate_before_and_after_the_decision(
    engine: AsyncEngine,
    alice: SeededUser,
    hook_dir: Path,
    spawn,
) -> None:
    tid = await start(engine, alice, "proposes_email")
    kill_in(spawn, hook_dir, tid, "action_gate", "w-A")
    resume_on(spawn, hook_dir, tid, "w-B")
    assert await status(engine, tid) == "awaiting_approval"
    key = await email_key(tid)
    await decide(engine, tid, "approve_action", {"action": "approve"})
    kill_in(spawn, hook_dir, tid, "action_gate_resumed", "w-C")
    resume_on(spawn, hook_dir, tid, "w-D")
    await assert_clean(engine, tid, "completed", ["action.decided", "action.outcome"])
    assert await mails_with_key(key) == 1


async def test_kill_at_escalation_gate_before_and_after_the_decision(
    engine: AsyncEngine,
    alice: SeededUser,
    hook_dir: Path,
    spawn,
) -> None:
    tid = await start(engine, alice, "revisions_exhausted")
    kill_in(spawn, hook_dir, tid, "escalation_gate", "w-A")
    resume_on(spawn, hook_dir, tid, "w-B")
    assert await status(engine, tid) == "awaiting_approval"
    await decide(engine, tid, "escalation", {"action": "edit", "edits": {"draft": "Human draft."}})
    kill_in(spawn, hook_dir, tid, "escalation_gate_resumed", "w-C")
    resume_on(spawn, hook_dir, tid, "w-D")
    await assert_clean(engine, tid, "completed", ["escalation.requested", "escalation.resolved"])


async def test_killed_after_approval_before_execution_sends_exactly_once(
    engine: AsyncEngine,
    alice: SeededUser,
    hook_dir: Path,
    spawn,
) -> None:
    tid = await start(engine, alice, "proposes_email")
    resume_on(spawn, hook_dir, tid, "w-A")  # runs to the action approval
    key = await email_key(tid)
    await decide(engine, tid, "approve_action", {"action": "approve"})
    kill_in(spawn, hook_dir, tid, "execute_actions", "w-B")  # dies before anything executes
    assert await mails_with_key(key) == 0
    resume_on(spawn, hook_dir, tid, "w-C")
    await assert_clean(engine, tid, "completed", ["action.outcome"])
    assert await mails_with_key(key) == 1
    assert (
        await scalar(
            engine, "SELECT status::text FROM side_effects WHERE idempotency_key=:k", k=key
        )
        == "done"
    )


async def test_killed_after_send_before_mark_done_is_surfaced_not_resent(
    engine: AsyncEngine,
    alice: SeededUser,
    hook_dir: Path,
    spawn,
) -> None:
    tid = await start(engine, alice, "proposes_email")
    resume_on(spawn, hook_dir, tid, "w-A")
    key = await email_key(tid)
    await decide(engine, tid, "approve_action", {"action": "approve"})
    kill_in(spawn, hook_dir, tid, "execute_after_perform", "w-B")  # sent, not marked done
    assert await mails_with_key(key) == 1
    resume_on(spawn, hook_dir, tid, "w-C")
    await assert_clean(engine, tid, "completed_partial", ["action.outcome"])
    assert await mails_with_key(key) == 1  # never resent
    assert (
        await scalar(
            engine, "SELECT status::text FROM side_effects WHERE idempotency_key=:k", k=key
        )
        == "in_progress"
    )
    review = await scalar(
        engine,
        "SELECT level || ':' || status::text || ':' || blocking::text FROM approvals "
        "WHERE task_id=:t AND key=:k",
        t=tid,
        k=f"side_effect:{key}",
    )
    assert review == "side_effect_review:pending:false"
    [o] = (await graph_state_values(tid))["action_outcomes"].values()
    assert o.status == "unknown"
