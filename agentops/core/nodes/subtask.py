"""subtask_worker: compiled subgraph, one instance per dispatched subtask (ADR-001).

    specialist --tool calls--> tools --> specialist ...   (ReAct, one attempt)
        |                        '-- step cap exceeded --> close_attempt   (failed attempt)
        '--final answer--> review --> close_attempt
    close_attempt: accept                      -> finish(accepted)
                   revise or step cap, revisions left -> specialist (new attempt, with a note)
                   revise or step cap at the cap       -> finish(needs_human)
                   escalate                            -> finish(needs_human)
    any ModelCallError                         -> finish(failed)

Cycle bounds (counter-based, model-independent): at most MAX_REVISIONS + 1 attempts, and at
most BUDGET__MAX_STEPS_PER_SPECIALIST tool calls per attempt. Hitting the step cap ends the
attempt as a failed attempt with a clear reason; it never raises (Phase 3 change 5).

Runs in parallel branches: no interrupt() and no external side effects here (DECISIONS
invariant). Tools are called only through the registry, which turns anything that would
affect the world outside the task into a ProposedAction instead of running it (ADR-003).
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import (
    AttemptRecord,
    ProposedAction,
    Review,
    ReviewVerdict,
    RunContext,
    SubtaskInput,
    SubtaskOutput,
    SubtaskResult,
    SubtaskState,
    ToolLogEntry,
)
from agentops.core.testhooks import block_if_hooked
from agentops.models.client import ModelCallError
from agentops.tools.native.workspace import task_workspace
from agentops.tools.registry import ToolRegistry
from agentops.tools.spec import ToolContext

MAX_REVISIONS = 2


def _coords(state: SubtaskState) -> tuple[str, str, str]:
    return (f"w{state['wave']}", state["subtask"].id, f"r{state.get('revision', 0)}")


def _registry(runtime: Runtime[RunContext]) -> ToolRegistry:
    reg = runtime.context.tools
    if reg is None:
        raise RuntimeError("the main graph needs a ToolRegistry in RunContext")
    return reg


def _max_steps(runtime: Runtime[RunContext]) -> int:
    return model_of(runtime).settings.budget.max_steps_per_specialist


async def specialist(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    sub = state["subtask"]
    rev = state.get("revision", 0)
    messages: list[BaseMessage] = list(state.get("messages") or [])
    if not messages:  # a new attempt
        await node_event(
            runtime, "specialist", "started", *_coords(state), payload={"subtask_id": sub.id}
        )
        await block_if_hooked(str(runtime.context.task_id), "specialist")
        messages = prompts.specialist_messages(
            sub,
            state["request"],
            state["context"],
            history=state.get("history", []),
            max_steps=_max_steps(runtime),
        )
    try:
        res = await model_of(runtime).invoke(
            sub.specialist,
            messages,
            task_id=runtime.context.task_id,
            purpose=f"act/r{rev}/{sub.id}",
            tools=_registry(runtime).model_tools(sub.specialist),
        )
    except ModelCallError as exc:
        return {"failed_reason": f"specialist failed: {exc}"}
    ai = res.message
    messages.append(ai)
    if ai.tool_calls:
        return {"messages": messages}
    draft = str(ai.text)
    await node_event(
        runtime,
        "specialist",
        "completed",
        *_coords(state),
        payload={"subtask_id": sub.id, "chars": len(draft), "tool_calls": state.get("step", 0)},
    )
    return {"messages": messages, "draft": draft, "attempt_outcome": "reviewed"}


def after_specialist(state: SubtaskState) -> Literal["tools", "review", "finish"]:
    if state.get("failed_reason"):
        return "finish"
    if state.get("attempt_outcome") == "reviewed":
        return "review"
    return "tools"


async def tools(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    sub = state["subtask"]
    rev = state.get("revision", 0)
    step = state.get("step", 0)
    cap = _max_steps(runtime)
    ai = state["messages"][-1]
    assert isinstance(ai, AIMessage) and ai.tool_calls
    calls = ai.tool_calls
    if step + len(calls) > cap:
        # Ends this attempt as failed (counts toward revisions); the calls are not executed.
        await node_event(
            runtime,
            "tools",
            "step_cap",
            *_coords(state),
            type_="specialist.step_cap",
            payload={"subtask_id": sub.id, "steps_used": step, "cap": cap, "requested": len(calls)},
        )
        return {"attempt_outcome": "step_cap"}

    registry = _registry(runtime)
    ctx_base = runtime.context
    settings = model_of(runtime).settings
    workspace = task_workspace(settings, ctx_base.task_id)
    new_messages: list[BaseMessage] = []
    log = list(state.get("tool_log", []))
    proposals: dict[str, ProposedAction] = {}
    for i, call in enumerate(calls):
        ctx = ToolContext(
            task_id=ctx_base.task_id,
            owner_id=ctx_base.owner_id,
            agent=sub.specialist,
            subtask_id=sub.id,
            revision=rev,
            step=step + i + 1,
            workspace=workspace,
            settings=settings,
            engine=ctx_base.engine,
        )
        inv = await registry.invoke(
            call["name"],
            dict(call["args"]),
            ctx,
            tool_call_id=call.get("id"),
            rationale=str(ai.text) or None,
        )
        new_messages.append(
            ToolMessage(inv.content, tool_call_id=call.get("id") or f"call_{i}", name=call["name"])
        )
        preview = inv.output.text if inv.output is not None else inv.content
        log.append(
            ToolLogEntry(
                step=step + i + 1,
                tool=call["name"],
                status=inv.status,
                args=dict(call["args"]),
                preview=preview[:500],
            )
        )
        if inv.proposal is not None:
            proposals[inv.proposal.hash] = inv.proposal
    out: dict[str, Any] = {
        "messages": [*state["messages"], *new_messages],
        "step": step + len(calls),
        "tool_log": log,
    }
    if proposals:
        out["proposed_actions"] = proposals
    return out


def after_tools(state: SubtaskState) -> Literal["specialist", "close_attempt"]:
    return "close_attempt" if state.get("attempt_outcome") == "step_cap" else "specialist"


async def review(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    sub = state["subtask"]
    revision = state.get("revision", 0)
    local = state.get("local_reviews", [])
    msgs = prompts.reviewer_messages(
        sub,
        state["request"],
        history=state.get("history", []),
        draft=state.get("draft") or "",
        tool_log=state.get("tool_log", []),
    )
    try:
        res = await model_of(runtime).invoke(
            "reviewer",
            msgs,
            task_id=runtime.context.task_id,
            purpose=f"review/r{revision}/{sub.id}",
            schema=ReviewVerdict,
        )
    except ModelCallError as exc:
        return {"failed_reason": f"review failed: {exc}"}
    assert isinstance(res.parsed, ReviewVerdict)
    await node_event(
        runtime,
        "review",
        "verdict",
        *_coords(state),
        type_="review.verdict",
        payload={"subtask_id": sub.id, "revision": revision, **res.parsed.model_dump()},
    )
    return {
        "local_reviews": [*local, Review(subtask_id=sub.id, revision=revision, verdict=res.parsed)]
    }


def after_review(state: SubtaskState) -> Literal["close_attempt", "finish"]:
    return "finish" if state.get("failed_reason") else "close_attempt"


def record_attempt(state: SubtaskState, cap: int) -> dict[str, Any]:
    """Pure: record how the attempt ended; start a new attempt if allowed (revise or step cap,
    and revisions remain). Returns the state update."""
    rev = state.get("revision", 0)
    if state.get("attempt_outcome") == "step_cap":
        rec = AttemptRecord(
            revision=rev,
            outcome="step_cap",
            note=prompts.attempt_note("step_cap", verdict=None, max_steps=cap),
        )
        retry = True
    else:
        verdict = state["local_reviews"][-1].verdict
        rec = AttemptRecord(
            revision=rev,
            outcome="reviewed",
            draft=state.get("draft") or "",
            verdict=verdict,
            note=prompts.attempt_note("reviewed", verdict=verdict, max_steps=cap),
        )
        retry = verdict.verdict == "revise"
    out: dict[str, Any] = {"history": [*state.get("history", []), rec]}
    if retry and rev < MAX_REVISIONS:
        out.update(
            revision=rev + 1,
            messages=[],
            step=0,
            tool_log=[],
            draft=None,
            attempt_outcome=None,
        )
    return out


async def close_attempt(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    out = record_attempt(state, _max_steps(runtime))
    rec: AttemptRecord = out["history"][-1]
    await node_event(
        runtime,
        "close_attempt",
        "closed",
        *_coords(state),
        type_="attempt.closed",
        payload={
            "subtask_id": state["subtask"].id,
            "revision": rec.revision,
            "outcome": rec.outcome,
            "verdict": rec.verdict.verdict if rec.verdict else None,
        },
    )
    return out


def after_close(state: SubtaskState) -> Literal["specialist", "finish"]:
    last = state["history"][-1]
    return "specialist" if last.revision < state.get("revision", 0) else "finish"


def terminal_result(state: SubtaskState) -> SubtaskResult:
    """Map the subgraph's final state to exactly one terminal status (ADR-002)."""
    sub = state["subtask"]
    revision = state.get("revision", 0)
    history = state.get("history", [])
    drafts = [r.draft for r in history if r.draft is not None]
    output = drafts[-1] if drafts else None
    if state.get("failed_reason"):
        return SubtaskResult(
            subtask_id=sub.id,
            status="failed",
            output=output or state.get("draft"),
            revisions=revision,
            reason=state["failed_reason"],
        )
    last = history[-1]
    if last.outcome == "reviewed" and last.verdict is not None:
        if last.verdict.verdict == "accept":
            return SubtaskResult(
                subtask_id=sub.id, status="accepted", output=last.draft, revisions=revision
            )
        reason = (
            "reviewer escalated"
            if last.verdict.verdict == "escalate"
            else f"revisions exhausted ({MAX_REVISIONS})"
        )
    else:
        reason = (
            f"step cap reached on the final attempt ({MAX_REVISIONS + 1} attempts, "
            "no final answer within the tool-call limit)"
        )
    return SubtaskResult(
        subtask_id=sub.id, status="needs_human", output=output, revisions=revision, reason=reason
    )


async def finish(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    result = terminal_result(state)
    await node_event(
        runtime,
        "subtask",
        "finished",
        f"w{state['wave']}",
        result.subtask_id,
        type_="subtask.finished",
        payload=result.model_dump(exclude={"output"}),
    )
    return {
        "results": {result.subtask_id: result},
        "reviews": state.get("local_reviews", []),
        "proposed_actions": state.get("proposed_actions", {}),
    }


def build_subtask_graph() -> CompiledStateGraph[
    SubtaskState, RunContext, SubtaskInput, SubtaskOutput
]:
    g = StateGraph(
        SubtaskState,
        context_schema=RunContext,
        input_schema=SubtaskInput,
        output_schema=SubtaskOutput,
    )
    g.add_node("specialist", specialist)
    g.add_node("tools", tools)
    g.add_node("review", review)
    g.add_node("close_attempt", close_attempt)
    g.add_node("finish", finish)
    g.add_edge(START, "specialist")
    g.add_conditional_edges("specialist", after_specialist, ["tools", "review", "finish"])
    g.add_conditional_edges("tools", after_tools, ["specialist", "close_attempt"])
    g.add_conditional_edges("review", after_review, ["close_attempt", "finish"])
    g.add_conditional_edges("close_attempt", after_close, ["specialist", "finish"])
    g.add_edge("finish", END)
    return g.compile()
