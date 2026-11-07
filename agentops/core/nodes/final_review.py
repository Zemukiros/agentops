"""final_review: the reviewer scores every deliverable (ADR-029 as amended in Phase 4).

score >= FINAL_REVIEW_MIN_SCORE -> completed (or completed_partial if subtasks or actions are
missing). Below it -> escalation_gate in final-review mode: a human approves, edits, takes
over or rejects. A failed review call -> failed.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.nodes.synthesize import delivery_outcome
from agentops.core.state import Outcome, ReviewVerdict, RunContext, TaskState
from agentops.models.client import ModelCallError


async def final_review(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None
    model = model_of(runtime)
    threshold = model.settings.final_review_min_score
    msgs = prompts.final_review_messages(
        state["request"], plan, state.get("final") or "", partial=bool(state.get("partial"))
    )
    try:
        res = await model.invoke(
            "reviewer",
            msgs,
            task_id=runtime.context.task_id,
            purpose="final_review",
            schema=ReviewVerdict,
        )
    except ModelCallError as exc:
        reason = f"final review failed: {exc}"
        return {"outcome": Outcome(status="failed", reason=reason)}
    assert isinstance(res.parsed, ReviewVerdict)
    verdict = res.parsed
    await node_event(
        runtime,
        "final_review",
        "verdict",
        type_="final_review.verdict",
        payload={**verdict.model_dump(), "threshold": threshold},
    )
    if verdict.score >= threshold:
        return {
            "final_review": verdict,
            "final_review_failed": False,
            "outcome": delivery_outcome(state),
        }
    return {"final_review": verdict, "final_review_failed": True}


def after_final_review(state: TaskState) -> Literal["escalation_gate", "consolidate"]:
    return "escalation_gate" if state.get("final_review_failed") else "consolidate"
