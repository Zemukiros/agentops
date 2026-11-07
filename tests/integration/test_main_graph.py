"""The `main` graph, end to end through the worker (in-process), in fake mode.

Every scenario must end in exactly one terminal state (or a pending interrupt), with the
expected events, model calls and budget accounting.
"""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops import approvals
from agentops.core import graph as graph_mod
from agentops.core import prompts
from agentops.core.graph import GraphSpec, build_main_graph
from agentops.core.nodes import subtask as subtask_mod
from agentops.core.state import RunContext
from agentops.db.checkpointer import open_checkpointer
from agentops.models.client import _digest
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import Budget, Settings, get_settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration

REQUEST = "Write a short summary of what pgvector is."


@pytest.fixture
async def saver(test_db: str) -> AsyncIterator[Any]:
    async with open_checkpointer(get_settings().libpq_dsn, max_size=4) as s:
        yield s


async def run_job(
    engine: AsyncEngine,
    saver: Any,
    tid: uuid.UUID,
    *,
    kind: str = "run",
    payload: dict | None = None,
    settings: Settings | None = None,
) -> None:
    """Enqueue one job for the task and let an in-process worker handle it."""
    await jobs.enqueue(engine, kind, thread_id=tid, payload=payload)
    s = settings or get_settings()
    worker = Worker(s, engine, saver, "w-test")
    job = await jobs.claim(engine, "w-test", s.job_lease_seconds)
    assert job is not None
    await worker.process(job)


async def process_next(engine: AsyncEngine, saver: Any, settings: Settings | None = None) -> None:
    """Let an in-process worker handle the next queued job (e.g. a decision's resume)."""
    s = settings or get_settings()
    job = await jobs.claim(engine, "w-test", s.job_lease_seconds)
    assert job is not None, "no job queued"
    await Worker(s, engine, saver, "w-test").process(job)


async def decide_pending(
    engine: AsyncEngine, tid: uuid.UUID, kind: str, raw: dict, user_id: uuid.UUID | None = None
) -> dict:
    """Decide the task's pending approval of `kind` exactly as the API does (validate, then the
    version-checked decide + apply in one transaction)."""
    async with engine.connect() as c:
        row = (
            await c.execute(
                text(
                    "SELECT id, version, payload FROM approvals WHERE task_id=:t AND level=:k "
                    "AND status='pending'"
                ),
                {"t": tid, "k": kind},
            )
        ).one()
    decision = approvals.validate_decision(kind, raw, dict(row.payload), get_settings())
    status = approvals.STATUS_FOR_ACTION[decision["action"]]
    async with engine.begin() as c:
        won = await approvals.decide(
            c,
            row.id,
            expected_version=row.version,
            decision=decision,
            status=status,
            decided_by=user_id,
        )
        assert won is not None
        await approvals.apply(c, won, decision, status)
    return decision


async def new_task(
    engine: AsyncEngine, user: SeededUser, scenario: str, budget: Budget | None = None
) -> uuid.UUID:
    return await create_task(
        engine, user, REQUEST, graph="main", config={"scenario": scenario}, budget=budget
    )


async def rows(engine: AsyncEngine, sql: str, **params: object) -> list[tuple]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(text(sql), params)]


async def task_row(engine: AsyncEngine, tid: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as c:
        r = (
            await c.execute(
                text("SELECT status::text AS status, result, error FROM tasks WHERE id = :t"),
                {"t": tid},
            )
        ).one()
    return dict(r._mapping)


async def event_types(engine: AsyncEngine, tid: uuid.UUID) -> list[str]:
    return [
        r[0]
        for r in await rows(engine, "SELECT type FROM events WHERE task_id=:t ORDER BY id", t=tid)
    ]


async def calls(engine: AsyncEngine, tid: uuid.UUID) -> list[tuple[str, str, int, bool]]:
    return await rows(
        engine,
        "SELECT role, split_part(purpose, '/', 1), attempt, ok FROM llm_calls "
        "WHERE task_id=:t ORDER BY id",
        t=tid,
    )


async def graph_values(saver: Any, tid: uuid.UUID) -> Mapping[str, Any]:
    cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    return (await build_main_graph(saver).aget_state(cfg)).values


async def assert_single_terminal(engine: AsyncEngine, tid: uuid.UUID, status: str) -> None:
    finished = await rows(
        engine,
        "SELECT payload->>'status' FROM events WHERE task_id=:t AND type='task.finished'",
        t=tid,
    )
    assert finished == [(status,)]
    assert (await event_types(engine, tid))[-1] == "task.finished"
    assert (await task_row(engine, tid))["status"] == status
    # every reservation was settled: nothing is left held after a run
    open_ = await rows(
        engine,
        "SELECT count(*) FROM budget_reservations WHERE task_id=:t AND status='reserved'",
        t=tid,
    )
    assert open_ == [(0,)]


# ---- happy paths ----------------------------------------------------------------------------
async def test_default_completes_with_final_output(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed")
    t = await task_row(engine, tid)
    assert t["result"]["final"].startswith("Final deliverable")
    assert t["result"]["subtasks"]["draft"]["status"] == "accepted"
    assert t["result"]["budget"]["llm_calls"] == 5  # plan, act, review, synthesize, final_review
    assert await calls(engine, tid) == [
        ("supervisor", "plan", 1, True),
        ("writing", "act", 1, True),
        ("reviewer", "review", 1, True),
        ("supervisor", "synthesize", 1, True),
        ("reviewer", "final_review", 1, True),
    ]
    ev = await event_types(engine, tid)
    for expected in ("plan.created", "subtask.dispatched", "review.verdict", "subtask.finished"):
        assert ev.count(expected) == 1, (expected, ev)


async def test_revise_once_then_accept(engine: AsyncEngine, saver: Any, alice: SeededUser) -> None:
    tid = await new_task(engine, alice, "revise_once")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed")
    sub = (await task_row(engine, tid))["result"]["subtasks"]["draft"]
    assert (sub["status"], sub["revisions"]) == ("accepted", 1)
    purposes = [p for _, p, _, _ in await calls(engine, tid)]
    assert purposes == ["plan", "act", "review", "act", "review", "synthesize", "final_review"]
    verdicts = await rows(
        engine,
        "SELECT payload->>'verdict', (payload->>'revision')::int FROM events "
        "WHERE task_id=:t AND type='review.verdict' ORDER BY id",
        t=tid,
    )
    assert verdicts == [("revise", 0), ("accept", 1)]
    values = await graph_values(saver, tid)
    assert values["results"]["draft"].output.startswith("Draft v2")


async def test_chain_runs_dependencies_in_waves(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "chain")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed")
    waves = await rows(
        engine,
        "SELECT payload->>'subtask_id', (payload->>'wave')::int FROM events "
        "WHERE task_id=:t AND type='subtask.dispatched' ORDER BY id",
        t=tid,
    )
    assert waves == [("outline", 1), ("draft", 2)]


async def test_bad_plan_is_replanned_once(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "bad_plan_then_fixed")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed")
    invalid = await rows(
        engine,
        "SELECT payload->'errors'->>0 FROM events WHERE task_id=:t AND type='plan.invalid'",
        t=tid,
    )
    assert len(invalid) == 1 and "cycle" in invalid[0][0]
    assert [p for _, p, _, _ in await calls(engine, tid)][:2] == ["plan", "plan"]
    assert (await graph_values(saver, tid))["plan_attempts"] == 2


# ---- unhappy paths --------------------------------------------------------------------------
async def test_two_invalid_plans_fail(engine: AsyncEngine, saver: Any, alice: SeededUser) -> None:
    tid = await new_task(engine, alice, "bad_plan_twice")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "failed")
    t = await task_row(engine, tid)
    assert "no valid plan after 2 attempts" in t["error"]
    assert "unknown ids ['ghost']" in t["error"]  # the second plan's error
    assert [p for _, p, _, _ in await calls(engine, tid)] == ["plan", "plan"]  # no more spend


async def test_specialist_error_fails_the_subtask_and_the_run(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "specialist_error")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "failed")
    t = await task_row(engine, tid)
    assert t["result"]["subtasks"]["draft"]["status"] == "failed"
    assert "no subtask was accepted; failed or blocked: ['draft']" in t["error"]
    writing = [(a, ok) for r, _, a, ok in await calls(engine, tid) if r == "writing"]
    assert writing == [(1, False), (2, False), (3, False)]  # every retry recorded


async def test_budget_exhaustion_pauses_then_reject_ends_budget_exceeded(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default", budget=Budget(max_llm_calls=2))
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"  # not a terminal: a reviewer decides
    need = t["result"]["approval_required"]
    assert need["kind"] == "budget" and "llm call limit" in need["reason"]
    assert len(await calls(engine, tid)) == 2  # the third call was never made
    await decide_pending(engine, tid, "budget", {"action": "reject"})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "budget_exceeded")
    t = await task_row(engine, tid)
    assert "rejected" in t["error"] and t["result"]["budget"]["llm_calls"] == 2


async def test_budget_raise_continues_from_the_checkpoint(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default", budget=Budget(max_llm_calls=2))
    await run_job(engine, saver, tid)
    with pytest.raises(approvals.DecisionError, match="above the current limit"):
        approvals.validate_decision(
            "budget",
            {"action": "approve", "max_llm_calls": 2},
            {"max_llm_calls": 2},
            get_settings(),
        )
    await decide_pending(engine, tid, "budget", {"action": "approve", "max_llm_calls": 20})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "completed")
    base = [p for _, p, _, _ in await calls(engine, tid)]
    assert base.count("plan") == 1  # continued from the checkpoint, not restarted
    assert "budget.raised" in await event_types(engine, tid)


async def test_recursion_limit_pauses_for_take_over(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    tight = get_settings().model_copy(
        update={"budget": get_settings().budget.model_copy(update={"recursion_limit": 3})}
    )
    await run_job(engine, saver, tid, settings=tight)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"  # not a crash
    assert t["result"]["approval_required"]["kind"] == "recursion"
    await decide_pending(
        engine, tid, "recursion", {"action": "take_over", "deliverable": "Human-written answer."}
    )
    await process_next(engine, saver, tight)
    await assert_single_terminal(engine, tid, "completed")
    r = (await task_row(engine, tid))["result"]
    assert r["final"] == "Human-written answer." and r["taken_over"] is True


async def test_recursion_limit_continue_then_reject(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    tight = get_settings().model_copy(
        update={"budget": get_settings().budget.model_copy(update={"recursion_limit": 3})}
    )
    await run_job(engine, saver, tid, settings=tight)
    await decide_pending(engine, tid, "recursion", {"action": "approve"})
    await process_next(engine, saver, tight)  # continues 3 more steps, pauses again
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    async with engine.connect() as c:
        keys = [
            r[0]
            for r in await c.execute(
                text("SELECT key FROM approvals WHERE task_id=:t ORDER BY created_at"), {"t": tid}
            )
        ]
    assert keys == ["recursion:x1", "recursion:x2"]
    await decide_pending(engine, tid, "recursion", {"action": "reject"})
    await process_next(engine, saver, tight)
    await assert_single_terminal(engine, tid, "failed")


async def test_graph_without_outcome_is_marked_failed(
    engine: AsyncEngine, saver: Any, alice: SeededUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-silent guard: a graph that reaches END without setting `outcome`."""

    def build(cp: Any) -> Any:
        g = StateGraph(dict, context_schema=RunContext)  # type: ignore[type-var]
        g.add_node("noop", lambda s: {"x": 1})
        g.add_edge(START, "noop")
        g.add_edge("noop", END)
        return g.compile(checkpointer=cp)

    monkeypatch.setitem(
        graph_mod.GRAPHS,
        "silent",
        GraphSpec(build, graph_mod.main_input, graph_mod.main_result, needs_model=False),
    )
    tid = await create_task(engine, alice, REQUEST, graph="silent")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "failed")
    assert (await task_row(engine, tid))["error"] == "graph ended without an outcome"


# ---- escalation (interrupt + resume) --------------------------------------------------------
async def _exhausted(engine: AsyncEngine, saver: Any, user: SeededUser) -> uuid.UUID:
    tid = await new_task(engine, user, "revisions_exhausted")
    await run_job(engine, saver, tid)
    return tid


async def test_revisions_exhausted_pauses_for_a_human(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _exhausted(engine, saver, alice)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    assert "task.finished" not in await event_types(engine, tid)
    [intr] = t["result"]["interrupts"]
    payload = intr["value"]
    assert payload["kind"] == "escalation" and payload["round"] == 1
    [sub] = payload["subtasks"]
    assert sub["subtask_id"] == "draft" and sub["reason"] == "revisions exhausted (2)"
    assert sub["draft"].startswith("Draft v3") and len(sub["reviews"]) == 3
    # the loop is capped: exactly 3 drafts and 3 reviews, then it stopped spending
    purposes = [p for _, p, _, _ in await calls(engine, tid)]
    assert purposes == ["plan"] + ["act", "review"] * 3
    assert (await graph_values(saver, tid))["results"]["draft"].status == "needs_human"


async def test_escalation_approve_completes(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _exhausted(engine, saver, alice)
    await run_job(engine, saver, tid, kind="resume", payload={"resume": {"action": "approve"}})
    await assert_single_terminal(engine, tid, "completed")
    sub = (await task_row(engine, tid))["result"]["subtasks"]["draft"]
    assert sub["status"] == "accepted" and sub["human_override"] is True
    assert "escalation.resolved" in await event_types(engine, tid)


async def test_escalation_edit_replaces_output(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _exhausted(engine, saver, alice)
    decision = {"action": "edit", "edits": {"draft": "Human-written draft."}}
    await run_job(engine, saver, tid, kind="resume", payload={"resume": decision})
    await assert_single_terminal(engine, tid, "completed")
    values = await graph_values(saver, tid)
    assert values["results"]["draft"].output == "Human-written draft."
    # synthesis was prompted with the human's text: recompute the expected prompt digest
    expected = _digest(
        prompts.synth_messages(values["request"], values["plan"], {"draft": "Human-written draft."})
    )
    assert await rows(
        engine,
        "SELECT request_digest FROM llm_calls WHERE task_id=:t AND purpose='synthesize'",
        t=tid,
    ) == [(expected,)]


async def test_escalation_reject_ends_rejected(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _exhausted(engine, saver, alice)
    decision = {"action": "reject", "note": "not what I asked for"}
    await run_job(engine, saver, tid, kind="resume", payload={"resume": decision})
    await assert_single_terminal(engine, tid, "rejected")
    assert (await task_row(engine, tid))["error"] == "not what I asked for"
    assert "synthesize" not in [p for _, p, _, _ in await calls(engine, tid)]


async def test_reviewer_escalate_goes_straight_to_a_human(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "reviewer_escalates")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    assert t["result"]["interrupts"][0]["value"]["subtasks"][0]["reason"] == "reviewer escalated"
    assert [p for _, p, _, _ in await calls(engine, tid)] == ["plan", "act", "review"]


async def test_resume_without_pending_interrupt_is_a_noop(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    await run_job(engine, saver, tid)
    before = await event_types(engine, tid)
    await run_job(engine, saver, tid, kind="resume", payload={"resume": {"action": "approve"}})
    assert await event_types(engine, tid) == before


# ---- invariants -----------------------------------------------------------------------------
def test_parallel_branch_code_never_interrupts() -> None:
    """DECISIONS invariant: no interrupt() inside the subtask_worker subgraph."""
    tree = ast.parse(inspect.getsource(subtask_mod))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "interrupt" not in names | attrs | imported


async def test_only_escalation_gate_interrupts(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _exhausted(engine, saver, alice)
    cfg: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    state = await build_main_graph(saver).aget_state(cfg)
    assert state.next == ("escalation_gate",)
    assert [t.name for t in state.tasks if t.interrupts] == ["escalation_gate"]


# ---- ADR-029: failures don't kill independent work; partial results ------------------------
async def test_partial_result_completed_partial(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    """intro (independent) is accepted, facts fails, summary (depends on facts) is blocked:
    the run ends completed_partial with a deliverable that lists what's missing."""
    tid = await new_task(engine, alice, "partial_ok")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed_partial")
    t = await task_row(engine, tid)
    subs = t["result"]["subtasks"]
    assert {k: v["status"] for k, v in subs.items()} == {
        "intro": "accepted",
        "facts": "failed",
        "summary": "blocked",
    }
    final = t["result"]["final"]
    assert "## What's missing" in final
    assert "**facts** (failed)" in final and "**summary** (blocked)" in final
    assert "Missing: A short markdown summary." in final
    assert t["result"]["partial"] is True
    assert t["result"]["final_review"]["score"] == 4
    assert "did not complete" in t["error"]  # the reason is recorded, not hidden
    # intro and facts were dispatched together; summary was never dispatched
    dispatched = await rows(
        engine,
        "SELECT payload->>'subtask_id', (payload->>'wave')::int FROM events "
        "WHERE task_id=:t AND type='subtask.dispatched' ORDER BY id",
        t=tid,
    )
    assert sorted(dispatched) == [("facts", 1), ("intro", 1)]
    blocked = await rows(
        engine,
        "SELECT payload->>'subtask_id' FROM events WHERE task_id=:t AND type='subtask.blocked'",
        t=tid,
    )
    assert blocked == [("summary",)]
    base = [p for _, p, _, _ in await calls(engine, tid)]
    assert base.count("synthesize") == 1 and base.count("final_review") == 1


async def test_partial_below_threshold_escalates_then_reject(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    """ADR-029 as amended in Phase 4: a low final review escalates instead of failing."""
    tid = await new_task(engine, alice, "partial_low_score")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    [intr] = t["result"]["interrupts"]
    v = intr["value"]
    assert (v["kind"], v["key"], v["partial"], v["review"]["score"]) == (
        "final_review",
        "final:f1",
        True,
        2,
    )
    assert "## What's missing" in v["deliverable"]
    await decide_pending(engine, tid, "final_review", {"action": "reject", "note": "not useful"})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "rejected")


async def test_partial_below_threshold_human_accepts(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "partial_low_score")
    await run_job(engine, saver, tid)
    await decide_pending(engine, tid, "final_review", {"action": "approve"})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "completed_partial")
    assert (
        "accepted by a human after a final review score of 2/5"
        in (await task_row(engine, tid))["error"]
    )


async def test_nothing_usable_fails_without_synthesis(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "specialist_error")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "failed")
    base = [p for _, p, _, _ in await calls(engine, tid)]
    assert "synthesize" not in base and "final_review" not in base


async def test_complete_runs_are_final_reviewed_too(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "completed" and t["result"]["partial"] is False
    assert t["result"]["final_review"]["score"] == 4
    assert "## What's missing" not in t["result"]["final"]


@pytest.mark.parametrize(
    ("decision", "status", "final"),
    [
        (
            {"action": "approve"},
            "completed",
            "Final deliverable: the summary, with a concrete example.",
        ),
        (
            {"action": "edit", "deliverable": "Human-polished deliverable."},
            "completed",
            "Human-polished deliverable.",
        ),
        (
            {"action": "take_over", "deliverable": "Written by a human."},
            "completed",
            "Written by a human.",
        ),
        ({"action": "reject"}, "rejected", None),
    ],
)
async def test_low_final_review_escalates_for_complete_deliverables(
    engine: AsyncEngine,
    saver: Any,
    alice: SeededUser,
    decision: dict,
    status: str,
    final: str | None,
) -> None:
    tid = await new_task(engine, alice, "final_low")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    assert t["result"]["interrupts"][0]["value"]["partial"] is False
    await decide_pending(engine, tid, "final_review", decision)
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, status)
    if final is not None:
        assert (await task_row(engine, tid))["result"]["final"] == final


async def test_failure_plus_escalation_then_approve_is_partial(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "partial_with_escalation")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    [intr] = t["result"]["interrupts"]
    assert [s["subtask_id"] for s in intr["value"]["subtasks"]] == ["intro"]
    await run_job(engine, saver, tid, kind="resume", payload={"resume": {"action": "approve"}})
    await assert_single_terminal(engine, tid, "completed_partial")
    subs = (await task_row(engine, tid))["result"]["subtasks"]
    assert subs["intro"]["human_override"] is True and subs["summary"]["status"] == "blocked"


# ---- plan_gate (ADR-036) -------------------------------------------------------------------
async def test_confident_plans_pass_without_approval(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default")
    await run_job(engine, saver, tid)
    await assert_single_terminal(engine, tid, "completed")
    async with engine.connect() as c:
        n = (
            await c.execute(text("SELECT count(*) FROM approvals WHERE task_id=:t"), {"t": tid})
        ).scalar_one()
        conf = (
            await c.execute(
                text("SELECT payload FROM events WHERE task_id=:t AND type='plan.confidence'"),
                {"t": tid},
            )
        ).scalar_one()
    assert n == 0 and conf["needs_approval"] is False and conf["effective"] == 0.8


async def _low_plan(engine: AsyncEngine, saver: Any, user: SeededUser) -> uuid.UUID:
    tid = await new_task(engine, user, "low_confidence_plan")
    await run_job(engine, saver, tid)
    t = await task_row(engine, tid)
    assert t["status"] == "awaiting_approval"
    [intr] = t["result"]["interrupts"]
    assert intr["value"]["kind"] == "approve_plan" and intr["value"]["key"] == "plan:p1"
    assert intr["value"]["confidence"]["effective"] == 0.3
    return tid


async def test_plan_gate_approve(engine: AsyncEngine, saver: Any, alice: SeededUser) -> None:
    tid = await _low_plan(engine, saver, alice)
    await decide_pending(engine, tid, "approve_plan", {"action": "approve"})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "completed")
    assert [p for _, p, _, _ in await calls(engine, tid)].count("act") == 1


async def test_plan_gate_edit_runs_the_edited_plan(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await _low_plan(engine, saver, alice)
    sub = {"description": "d", "specialist": "writing", "expected_output": "e", "complexity": "low"}
    edited = {
        "subtasks": [{"id": "one", **sub}, {"id": "two", "depends_on": ["one"], **sub}],
        "confidence": 0.9,
        "rationale": "human edit",
    }
    with pytest.raises(approvals.DecisionError, match="cycle"):
        cyclic = {
            **edited,
            "subtasks": [
                {"id": "one", "depends_on": ["two"], **sub},
                {"id": "two", "depends_on": ["one"], **sub},
            ],
        }
        approvals.validate_decision(
            "approve_plan", {"action": "edit", "plan": cyclic}, {}, get_settings()
        )
    await decide_pending(engine, tid, "approve_plan", {"action": "edit", "plan": edited})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "completed")
    assert set((await task_row(engine, tid))["result"]["subtasks"]) == {"one", "two"}


async def test_plan_gate_reject(engine: AsyncEngine, saver: Any, alice: SeededUser) -> None:
    tid = await _low_plan(engine, saver, alice)
    await decide_pending(engine, tid, "approve_plan", {"action": "reject", "note": "too vague"})
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "rejected")
    assert (await task_row(engine, tid))["error"] == "too vague"
    assert "act" not in [p for _, p, _, _ in await calls(engine, tid)]


async def test_plan_gate_take_over(engine: AsyncEngine, saver: Any, alice: SeededUser) -> None:
    tid = await _low_plan(engine, saver, alice)
    await decide_pending(
        engine, tid, "approve_plan", {"action": "take_over", "deliverable": "I wrote it myself."}
    )
    await process_next(engine, saver)
    await assert_single_terminal(engine, tid, "completed")
    t = await task_row(engine, tid)
    assert t["result"]["final"] == "I wrote it myself."


async def test_budget_warning_fires_once_at_80_percent(
    engine: AsyncEngine, saver: Any, alice: SeededUser
) -> None:
    tid = await new_task(engine, alice, "default", budget=Budget(max_llm_calls=5))
    await run_job(engine, saver, tid)  # 5 calls: plan, act, review, synthesize, final_review
    await assert_single_terminal(engine, tid, "completed")
    async with engine.connect() as c:
        warnings = (
            (
                await c.execute(
                    text("SELECT payload FROM events WHERE task_id=:t AND type='budget.warning'"),
                    {"t": tid},
                )
            )
            .scalars()
            .all()
        )
    [w] = warnings  # once, although calls 4 and 5 are both >= 80%
    assert w["llm_calls"] == 4 and w["fraction_used"] == 0.8
