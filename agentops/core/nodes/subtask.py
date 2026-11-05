"""subtask_worker: compiled subgraph, one instance per dispatched subtask (ADR-001).

    specialist -> review -> accept               -> finish(accepted)
                         -> revise, revision < 2 -> specialist (with feedback)
                         -> revise at the cap    -> finish(needs_human)
                         -> escalate             -> finish(needs_human)
    any ModelCallError                           -> finish(failed)

Cycle bound: at most MAX_REVISIONS revisions, so at most 3 specialist and 3 review calls,
whatever the reviewer says. The counter lives in subgraph state, never in the parent.

Runs in parallel branches: no interrupt() and no external side effects here (DECISIONS
invariant). Events are internal and idempotent.

Phase 2: the writing specialist is a single model call. The ReAct tool loop and step cap
arrive with tools in Phase 3.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import (
    Review,
    ReviewVerdict,
    RunContext,
    SubtaskInput,
    SubtaskOutput,
    SubtaskResult,
    SubtaskState,
)
from agentops.core.testhooks import block_if_hooked
from agentops.models.client import ModelCallError

MAX_REVISIONS = 2


def _coords(state: SubtaskState) -> tuple[str, str, str]:
    return (f"w{state['wave']}", state["subtask"].id, f"r{state.get('revision', 0)}")


async def specialist(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    sub = state["subtask"]
    drafts = state.get("drafts", [])
    await node_event(
        runtime, "specialist", "started", *_coords(state), payload={"subtask_id": sub.id}
    )
    await block_if_hooked(str(runtime.context.task_id), "specialist")
    msgs = prompts.writer_messages(
        sub,
        state["request"],
        state["context"],
        drafts=drafts,
        reviews=state.get("local_reviews", []),
    )
    try:
        res = await model_of(runtime).invoke(
            sub.specialist, msgs, task_id=runtime.context.task_id, purpose="draft"
        )
    except ModelCallError as exc:
        return {"failed_reason": f"specialist failed: {exc}"}
    draft = res.message.text
    await node_event(
        runtime,
        "specialist",
        "completed",
        *_coords(state),
        payload={"subtask_id": sub.id, "chars": len(draft)},
    )
    return {"drafts": [*drafts, draft]}


async def review(state: SubtaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    sub = state["subtask"]
    revision = state.get("revision", 0)
    local = state.get("local_reviews", [])
    msgs = prompts.reviewer_messages(
        sub, state["request"], drafts=state.get("drafts", []), reviews=local
    )
    try:
        res = await model_of(runtime).invoke(
            "reviewer",
            msgs,
            task_id=runtime.context.task_id,
            purpose="review",
            schema=ReviewVerdict,
        )
    except ModelCallError as exc:
        return {"failed_reason": f"review failed: {exc}"}
    assert isinstance(res.parsed, ReviewVerdict)
    rec = Review(subtask_id=sub.id, revision=revision, verdict=res.parsed)
    await node_event(
        runtime,
        "review",
        "verdict",
        *_coords(state),
        type_="review.verdict",
        payload={"subtask_id": sub.id, "revision": revision, **res.parsed.model_dump()},
    )
    out: dict[str, Any] = {"local_reviews": [*local, rec]}
    if res.parsed.verdict == "revise" and revision < MAX_REVISIONS:
        out["revision"] = revision + 1
    return out


def after_specialist(state: SubtaskState) -> Literal["review", "finish"]:
    return "finish" if state.get("failed_reason") else "review"


def after_review(state: SubtaskState) -> Literal["specialist", "finish"]:
    if state.get("failed_reason"):
        return "finish"
    last = state["local_reviews"][-1]
    # `review` bumped `revision` only if a revise is still allowed.
    if last.verdict.verdict == "revise" and last.revision < state.get("revision", 0):
        return "specialist"
    return "finish"


def terminal_result(state: SubtaskState) -> SubtaskResult:
    """Map the subgraph's final state to exactly one terminal status (ADR-002)."""
    sub = state["subtask"]
    drafts = state.get("drafts", [])
    revision = state.get("revision", 0)
    output = drafts[-1] if drafts else None
    if state.get("failed_reason"):
        return SubtaskResult(
            subtask_id=sub.id,
            status="failed",
            output=output,
            revisions=revision,
            reason=state["failed_reason"],
        )
    last = state["local_reviews"][-1].verdict
    if last.verdict == "accept":
        return SubtaskResult(
            subtask_id=sub.id, status="accepted", output=output, revisions=revision
        )
    reason = (
        "reviewer escalated"
        if last.verdict == "escalate"
        else f"revisions exhausted ({MAX_REVISIONS})"
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
    return {"results": {result.subtask_id: result}, "reviews": state.get("local_reviews", [])}


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
    g.add_node("review", review)
    g.add_node("finish", finish)
    g.add_edge(START, "specialist")
    g.add_conditional_edges("specialist", after_specialist, ["review", "finish"])
    g.add_conditional_edges("review", after_review, ["specialist", "finish"])
    g.add_edge("finish", END)
    return g.compile()
