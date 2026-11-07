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
from agentops.core.state import Outcome, Plan, RunContext, SubtaskResult, TaskState
from agentops.models.client import ModelCallError


def missing_section(plan: Plan, results: dict[str, SubtaskResult]) -> str:
    lines = ["## What's missing", ""]
    lines.append(
        "This is a partial deliverable. These subtasks did not complete, so their parts "
        "are not included:"
    )
    lines.append("")
    for s in plan.subtasks:
        r = results.get(s.id)
        if r is None or r.status == "accepted":
            continue
        lines.append(
            f"- **{s.id}** ({r.status}): {r.reason or 'no reason recorded'}. "
            f"Missing: {s.expected_output}"
        )
    return "\n".join(lines)


async def synthesize(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None
    results = state.get("results", {})
    partial = any(r.status != "accepted" for r in results.values()) or len(results) < len(
        plan.subtasks
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
    final = f"{body.rstrip()}\n\n{missing_section(plan, results)}\n" if partial else body
    await node_event(
        runtime, "synthesize", "completed", payload={"chars": len(final), "partial": partial}
    )
    if partial:
        return {"final": final, "partial": True}
    return {"final": final, "partial": False, "outcome": Outcome(status="completed")}


def after_synthesize(state: TaskState) -> Literal["final_review", "__end__"]:
    if state.get("outcome") is not None:
        return "__end__"
    return "final_review"
