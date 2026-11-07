"""synthesize: compose the final deliverable from accepted outputs.

- All subtasks accepted -> outcome `completed`.
- Partial (ADR-029): the model writes the body from the accepted outputs only, and this node
  appends a "What's missing" section generated from state (not left to the model), listing
  every failed or blocked subtask, why, and what it was supposed to produce. No outcome is
  set here: `final_review` decides between `completed_partial` and `failed`.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import (
    ActionOutcome,
    Outcome,
    Plan,
    RunContext,
    SubtaskResult,
    TaskState,
)
from agentops.models.client import ModelCallError

OK_ACTION = frozenset({"done", "human_done"})


def unperformed_actions(state: TaskState) -> list[ActionOutcome]:
    """Approved-or-not actions that did not end done (by us or by a human)."""
    outcomes = state.get("action_outcomes", {}) or {}
    return [o for _, o in sorted(outcomes.items()) if o.status not in OK_ACTION]


def missing_section(
    plan: Plan, results: dict[str, SubtaskResult], actions: list[ActionOutcome] | None = None
) -> str:
    lines = ["## What's missing", ""]
    gaps = [s for s in plan.subtasks if (r := results.get(s.id)) is None or r.status != "accepted"]
    if gaps:
        lines.append(
            "This is a partial deliverable. These subtasks did not complete, so their parts "
            "are not included:"
        )
        lines.append("")
        for s in gaps:
            r = results.get(s.id)
            status = r.status if r else "not run"
            reason = (r.reason if r else None) or "no reason recorded"
            lines.append(f"- **{s.id}** ({status}): {reason}. Missing: {s.expected_output}")
    if actions:
        if gaps:
            lines.append("")
        lines.append("These proposed actions were not performed:")
        lines.append("")
        for o in actions:
            lines.append(f"- `{o.tool}` ({o.status}): {o.detail or 'no detail'}")
    return "\n".join(lines)


async def synthesize(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None
    results = state.get("results", {})
    actions = unperformed_actions(state)
    partial = (
        any(r.status != "accepted" for r in results.values())
        or len(results) < len(plan.subtasks)
        or bool(actions)
    )
    await node_event(runtime, "synthesize", "started", payload={"partial": partial})
    outputs = {sid: r.output or "" for sid, r in results.items() if r.status == "accepted"}
    msgs = prompts.synth_messages(state["request"], plan, outputs, partial=partial)
    try:
        res = await model_of(runtime).invoke(
            "supervisor", msgs, task_id=runtime.context.task_id, purpose="synthesize"
        )
    except ModelCallError as exc:
        reason = f"synthesis failed: {exc}"
        return {"failure_reason": reason, "outcome": Outcome(status="failed", reason=reason)}
    body: str = str(res.message.text)
    final = f"{body.rstrip()}\n\n{missing_section(plan, results, actions)}\n" if partial else body
    await node_event(
        runtime, "synthesize", "completed", payload={"chars": len(final), "partial": partial}
    )
    return {"final": final, "partial": partial}


def delivery_outcome(state: TaskState, *, note: str | None = None) -> Outcome:
    """completed if every subtask was accepted and every action done; otherwise
    completed_partial with what's missing (ADR-029, ADR-037)."""
    plan = state.get("plan")
    results = state.get("results", {})
    gaps = [
        sid
        for sid in ([s.id for s in plan.subtasks] if plan else [])
        if (r := results.get(sid)) is None or r.status != "accepted"
    ]
    actions = unperformed_actions(state)
    if not gaps and not actions:
        return Outcome(status="completed", reason=note)
    parts = []
    if gaps:
        parts.append(f"subtask(s) {gaps} did not complete")
    if actions:
        parts.append(f"actions not performed: {[f'{a.tool} ({a.status})' for a in actions]}")
    if note:
        parts.append(note)
    return Outcome(status="completed_partial", reason="; ".join(parts))


def after_synthesize(state: TaskState) -> Literal["final_review", "consolidate"]:
    if state.get("outcome") is not None:
        return "consolidate"
    return "final_review"
