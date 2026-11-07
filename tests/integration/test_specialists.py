"""ReAct specialists and parallel waves (Phase 3 steps 1-2), fake mode, through the worker."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.core.graph import build_main_graph
from agentops.db.checkpointer import open_checkpointer
from agentops.models.client import fake_factory
from agentops.models.fake import FakeScript, ScriptedTurn
from agentops.models.scenarios import build_scenarios
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Settings, get_settings
from agentops.tools.catalog import native_tools
from agentops.tools.registry import ToolRegistry
from agentops.tools.spec import ALL_SPECIALISTS, ToolContext, ToolInput, ToolOutput, ToolSpec
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return get_settings().model_copy(update={"workspace_root": tmp_path / "ws"})


def plan_turn(*subtasks: dict[str, Any]) -> ScriptedTurn:
    return ScriptedTurn(
        structured={"subtasks": list(subtasks), "confidence": 0.8, "rationale": "test"}
    )


def st(sid: str, specialist: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": sid,
        "description": f"do {sid}",
        "specialist": specialist,
        "depends_on": deps or [],
        "expected_output": f"{sid} output",
        "complexity": "low",
    }


def call(name: str, **args: Any) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[{"name": name, "args": args}])


def script_with(*adds: tuple) -> FakeScript:
    """The shared scenario defaults (accepting reviewer, synthesizer) plus test-specific turns."""
    s = build_scenarios()
    for role, purpose, turns in adds:
        s.add(role, "t", *turns, purpose=purpose)
    return s


async def run(
    engine: AsyncEngine,
    saver: Any,
    user: SeededUser,
    script: FakeScript,
    settings: Settings,
    tools: ToolRegistry | None = None,
) -> uuid.UUID:
    tid = await create_task(engine, user, "test request", graph="main")
    await jobs.enqueue(engine, "run", thread_id=tid)
    worker = Worker(
        settings, engine, saver, "w", chat_factory=fake_factory(script, "t"), tools=tools
    )
    job = await jobs.claim(engine, "w", settings.job_lease_seconds)
    assert job is not None
    await worker.process(job)
    return tid


async def q(engine: AsyncEngine, sql: str, **p: object) -> list[tuple]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(text(sql), p)]


async def status(engine: AsyncEngine, tid: uuid.UUID) -> tuple[str, Any, str | None]:
    [(s, r, e)] = await q(
        engine, "SELECT status::text, result, error FROM tasks WHERE id=:t", t=tid
    )
    return s, r, e


async def test_tool_loop_self_corrects_after_a_validation_error(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    script = script_with(
        ("supervisor", "plan", [plan_turn(st("notes", "writing"))]),
        (
            "writing",
            "act",
            [
                call("workspace_write", path="n.md", content="hello", mode="append"),  # bad arg
                call("workspace_write", path="n.md", content="hello"),
                call("workspace_read", path="n.md"),
                ScriptedTurn(content="Final: the note says hello."),
            ],
        ),
    )
    tid = await run(engine, saver, alice, script, settings)
    s, _, _ = await status(engine, tid)
    assert s == "completed"
    assert await q(
        engine,
        "SELECT tool, status, step FROM tool_calls WHERE task_id=:t ORDER BY id",
        t=tid,
    ) == [
        ("workspace_write", "validation_error", 1),
        ("workspace_write", "ok", 2),
        ("workspace_read", "ok", 3),
    ]
    assert (settings.workspace_root / str(tid) / "n.md").read_text() == "hello"
    # the model saw the validation error as a framed tool result and could fix it
    cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    values = (await build_main_graph(saver).aget_state(cfg)).values
    assert values["results"]["notes"].output == "Final: the note says hello."


async def test_step_cap_is_a_failed_attempt_then_recovers(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    capped = settings.model_copy(
        update={"budget": settings.budget.model_copy(update={"max_steps_per_specialist": 3})}
    )
    script = script_with(
        ("supervisor", "plan", [plan_turn(st("r", "research"))]),
        ("research", "act/r0", [call("workspace_list")]),  # never answers in attempt 0
        ("research", "act/r1", [ScriptedTurn(content="answer on the second attempt")]),
    )
    tid = await run(engine, saver, alice, script, capped)
    s, result, _ = await status(engine, tid)
    assert s == "completed"
    assert result["subtasks"]["r"]["revisions"] == 1
    calls = await q(engine, "SELECT revision, status FROM tool_calls WHERE task_id=:t", t=tid)
    assert calls == [(0, "ok")] * 3  # the 4th requested call was refused by the cap
    ev = await q(
        engine,
        "SELECT payload->>'outcome' FROM events WHERE task_id=:t AND type='attempt.closed' "
        "ORDER BY id",
        t=tid,
    )
    assert ev == [("step_cap",), ("reviewed",)]
    [(cap_ev,)] = await q(
        engine, "SELECT payload FROM events WHERE task_id=:t AND type='specialist.step_cap'", t=tid
    )
    assert cap_ev["cap"] == 3 and cap_ev["steps_used"] == 3


async def test_step_cap_on_every_attempt_needs_a_human(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    capped = settings.model_copy(
        update={"budget": settings.budget.model_copy(update={"max_steps_per_specialist": 2})}
    )
    script = script_with(
        ("supervisor", "plan", [plan_turn(st("r", "research"))]),
        ("research", "act", [call("workspace_list")]),
    )
    tid = await run(engine, saver, alice, script, capped)
    s, result, _ = await status(engine, tid)
    assert s == "awaiting_approval"  # no exception: a human decides
    sub = result["interrupts"][0]["value"]["subtasks"][0]
    assert "step cap reached on the final attempt" in sub["reason"]
    assert await q(engine, "SELECT count(*) FROM tool_calls WHERE task_id=:t", t=tid) == [(6,)]
    reviews = await q(
        engine, "SELECT count(*) FROM llm_calls WHERE task_id=:t AND purpose LIKE 'review/%'", t=tid
    )
    assert reviews == [(0,)]  # nothing to review: no attempt produced a draft


class BarrierIn(ToolInput):
    who: str


def barrier_registry(arrived: list[str]) -> ToolRegistry:
    """A read tool that only returns once both parallel branches are inside it. If the
    branches ran one after the other, the first would time out."""
    both = asyncio.Event()

    async def barrier(args: BarrierIn, ctx: ToolContext) -> ToolOutput:
        arrived.append(args.who)
        if len(arrived) >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return ToolOutput(text=f"{args.who} passed the barrier")

    spec = ToolSpec(
        name="barrier",
        description="wait for the other branch",
        input_model=BarrierIn,
        fn=barrier,
        allowed_agents=ALL_SPECIALISTS,
        risk="read",
        timeout_s=10,
    )
    return ToolRegistry([*native_tools(get_settings()), spec])


async def test_parallel_wave_runs_concurrently_and_merges_every_result(
    engine: AsyncEngine, saver: Any, alice: SeededUser, settings: Settings
) -> None:
    script = script_with(
        (
            "supervisor",
            "plan",
            [plan_turn(st("a", "research"), st("b", "analysis"), st("c", "writing", ["a", "b"]))],
        ),
        ("research", "act", [call("barrier", who="a"), ScriptedTurn(content="A found X")]),
        ("analysis", "act", [call("barrier", who="b"), ScriptedTurn(content="B measured Y")]),
        ("writing", "act", [ScriptedTurn(content="C combines X and Y")]),
    )
    arrived: list[str] = []
    tid = await run(engine, saver, alice, script, settings, tools=barrier_registry(arrived))
    s, result, err = await status(engine, tid)
    assert s == "completed", err
    assert sorted(arrived) == ["a", "b"]
    assert {k: v["status"] for k, v in result["subtasks"].items()} == {
        "a": "accepted",
        "b": "accepted",
        "c": "accepted",
    }
    waves = await q(
        engine,
        "SELECT payload->>'subtask_id', (payload->>'wave')::int FROM events "
        "WHERE task_id=:t AND type='subtask.dispatched' ORDER BY 1",
        t=tid,
    )
    assert waves == [("a", 1), ("b", 1), ("c", 2)]
    # reducers merged every branch's results and reviews; nothing overwritten
    cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    values = (await build_main_graph(saver).aget_state(cfg)).values
    assert values["results"]["a"].output == "A found X"
    assert values["results"]["b"].output == "B measured Y"
    assert sorted(r.subtask_id for r in values["reviews"]) == ["a", "b", "c"]
    calls = await q(
        engine,
        "SELECT subtask_id, agent, status FROM tool_calls WHERE task_id=:t ORDER BY subtask_id",
        t=tid,
    )
    assert calls == [("a", "research", "ok"), ("b", "analysis", "ok")]
