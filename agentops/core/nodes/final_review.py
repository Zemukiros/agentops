"""final_review: the reviewer scores a partial deliverable (ADR-029).

score >= FINAL_REVIEW_MIN_SCORE -> completed_partial; otherwise (or if the review call fails)
-> failed. The deliverable is kept in the result either way, for inspection.

Phase 3 runs this only for partial deliverables. The DECISIONS graph also reviews complete
deliverables (low score -> escalation); that arrives with the Phase 4 gates.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import Outcome, ReviewVerdict, RunContext, TaskState
from agentops.models.client import ModelCallError


async def final_review(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None
    model = model_of(runtime)
    threshold = model.settings.final_review_min_score
    msgs = prompts.final_review_messages(state["request"], plan, state.get("final") or "")
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
    decision = state.get("decision")
    missing = (decision.reason if decision is not None else None) or "partial deliverable"
    if verdict.score >= threshold:
        outcome = Outcome(status="completed_partial", reason=missing)
    else:
        outcome = Outcome(
            status="failed",
            reason=f"partial deliverable scored {verdict.score}/5, below {threshold}; {missing}",
        )
    return {"final_review": verdict, "outcome": outcome}
