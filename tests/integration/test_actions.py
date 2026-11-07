"""action_gate + execute_actions (ADR-004, ADR-037), fake models, real Mailpit."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops import approvals
from agentops.core.nodes.actions import idempotency_key
from agentops.db.checkpointer import open_checkpointer
from agentops.models.client import fake_factory
from agentops.models.fake import FakeScript, ScriptedTurn
from agentops.models.scenarios import build_scenarios
from agentops.queue import jobs
from agentops.settings import Settings, get_settings
from tests.integration.conftest import SeededUser, create_task, decide, graph_state_values, work

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return get_settings().model_copy(update={"workspace_root": tmp_path / "ws"})


def email_script(*emails: dict[str, str]) -> FakeScript:
    s = build_scenarios()
    plan = ScriptedTurn(
        structured={
            "subtasks": [
                {
                    "id": "w",
                    "description": "write and share",
                    "specialist": "writing",
                    "depends_on": [],
                    "expected_output": "summary",
                    "complexity": "low",
                }
            ],
            "confidence": 0.9,
            "rationale": "t",
        }
    )
    s.add("supervisor", "t", plan, purpose="plan")
    s.add(
        "writing",
        "t",
        ScriptedTurn(tool_calls=[{"name": "send_email", "args": e} for e in emails]),
        ScriptedTurn(content="Summary done."),
        purpose="act",
    )
    return s


async def start(
    engine: AsyncEngine, saver: Any, user: SeededUser, script: FakeScript, settings: Settings
) -> uuid.UUID:
    tid = await create_task(engine, user, "write and share", graph="main")
    await jobs.enqueue(engine, "run", thread_id=tid)
    await work(engine, saver, settings=settings, chat_factory=fake_factory(script, "t"))
    return tid


async def resume(engine: AsyncEngine, saver: Any, script: FakeScript, settings: Settings) -> None:
    await work(engine, saver, settings=settings, chat_factory=fake_factory(script, "t"))


async def mailpit(subject: str) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "http://localhost:8025/api/v1/search", params={"query": f'subject:"{subject}"'}
        )
    return r.json()["messages"]


async def task(engine: AsyncEngine, tid: uuid.UUID) -> tuple[str, dict, str | None]:
    async with engine.connect() as c:
        s, r, e = (
            await c.execute(
                text("SELECT status::text, result, error FROM tasks WHERE id=:t"), {"t": tid}
            )
        ).one()
    return s, r, e


def mail(subject: str, to: str = "team@example.com") -> dict[str, str]:
    return {"to": to, "subject": subject, "body": "hello"}


async def test_approve_sends_exactly_one_email_keyed_by_idempotency_key(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    subject = f"approve {uuid.uuid4().hex[:8]}"
    script = email_script(mail(subject))
    tid = await start(engine, saver, alice, script, settings)
    assert (await task(engine, tid))[0] == "awaiting_approval" and await mailpit(subject) == []
    await decide(engine, tid, "approve_action", {"action": "approve"})
    await resume(engine, saver, script, settings)
    status, _result, _ = await task(engine, tid)
    assert status == "completed"
    [msg] = await mailpit(subject)
    [p] = (await graph_state_values(tid))["proposed_actions"].values()
    key = idempotency_key(str(tid), p.hash, p.args)
    assert msg["MessageID"] == f"{key}@agentops"
    async with engine.connect() as c:
        row = (
            await c.execute(
                text("SELECT status::text, tool FROM side_effects WHERE idempotency_key=:k"),
                {"k": key},
            )
        ).one()
    assert tuple(row) == ("done", "send_email")


async def test_reject_executes_nothing_and_the_run_is_partial(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    subject = f"reject {uuid.uuid4().hex[:8]}"
    script = email_script(mail(subject))
    tid = await start(engine, saver, alice, script, settings)
    await decide(engine, tid, "approve_action", {"action": "reject", "note": "not now"})
    await resume(engine, saver, script, settings)
    status, result, _ = await task(engine, tid)
    assert status == "completed_partial"
    assert "`send_email` (rejected): not now" in result["final"]
    assert await mailpit(subject) == []
    async with engine.connect() as c:
        assert (
            await c.execute(text("SELECT count(*) FROM side_effects WHERE task_id=:t"), {"t": tid})
        ).scalar_one() == 0


async def test_edit_executes_the_edited_args(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    original = f"orig {uuid.uuid4().hex[:8]}"
    edited = f"edited {uuid.uuid4().hex[:8]}"
    script = email_script(mail(original))
    tid = await start(engine, saver, alice, script, settings)
    with pytest.raises(approvals.DecisionError, match="edited args are invalid"):
        await decide(
            engine,
            tid,
            "approve_action",
            {"action": "edit", "args": {"to": "not an address", "subject": "x", "body": "y"}},
        )
    await decide(
        engine,
        tid,
        "approve_action",
        {"action": "edit", "args": mail(edited, to="lead@example.com")},
    )
    await resume(engine, saver, script, settings)
    assert (await task(engine, tid))[0] == "completed"
    assert await mailpit(original) == []
    [msg] = await mailpit(edited)
    assert msg["To"][0]["Address"] == "lead@example.com"


async def test_take_over_records_human_done_and_sends_nothing(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    subject = f"takeover {uuid.uuid4().hex[:8]}"
    script = email_script(mail(subject))
    tid = await start(engine, saver, alice, script, settings)
    await decide(engine, tid, "approve_action", {"action": "take_over", "note": "I emailed them"})
    await resume(engine, saver, script, settings)
    assert (await task(engine, tid))[0] == "completed"
    assert await mailpit(subject) == []
    [o] = (await graph_state_values(tid))["action_outcomes"].values()
    assert (o.status, o.detail) == ("human_done", "I emailed them")


async def test_each_action_gets_its_own_approval_in_turn(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    s1, s2 = f"one {uuid.uuid4().hex[:8]}", f"two {uuid.uuid4().hex[:8]}"
    script = email_script(mail(s1), mail(s2))
    tid = await start(engine, saver, alice, script, settings)
    await decide(engine, tid, "approve_action", {"action": "approve"})
    await resume(engine, saver, script, settings)
    assert (await task(engine, tid))[0] == "awaiting_approval"  # second action now
    await decide(engine, tid, "approve_action", {"action": "reject"})
    await resume(engine, saver, script, settings)
    status, _, _ = await task(engine, tid)
    assert status == "completed_partial"
    assert len(await mailpit(s1)) + len(await mailpit(s2)) == 1
    async with engine.connect() as c:
        decided = (
            await c.execute(
                text("SELECT count(*) FROM approvals WHERE task_id=:t AND level='approve_action'"),
                {"t": tid},
            )
        ).scalar_one()
    assert decided == 2
