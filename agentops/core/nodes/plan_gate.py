"""plan_gate: a human approves low-confidence plans (ADR-036).

Serial node; only pure computation before interrupt() (the confidence was computed and
evented by `plan`). LangGraph re-runs this node from its first line on resume.

  confidence >= threshold -> continue (no interrupt)
  approve   -> continue with the plan
  edit      -> continue with the edited plan (re-validated as a DAG; the API checked it too)
  reject    -> finalize_rejected -> `rejected`
  take_over -> the human's deliverable; outcome `completed` (taken over)
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agentops.core import scheduler
from agentops.core.nodes.common import node_event
from agentops.core.state import Outcome, PlanDecision, RunContext, TaskState


def plan_payload(state: TaskState) -> dict[str, Any]:
    plan, conf = state.get("plan"), state.get("plan_confidence")
    assert plan is not None and conf is not None
    return {
        "kind": "approve_plan",
        "key": f"plan:p{state.get('plan_attempts', 1)}",
        "request": state["request"],
        "plan": plan.model_dump(mode="json"),
        "confidence": conf.model_dump(),
        "actions": ["approve", "edit", "reject", "take_over"],
    }


async def plan_gate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    conf = state.get("plan_confidence")
    if conf is None or not conf.needs_approval:
        return {"plan_decision": None}
    payload = plan_payload(state)  # pure
    decision: PlanDecision = interrupt(payload, response_schema=PlanDecision)
    await node_event(
        runtime,
        "plan_gate",
        "resolved",
        payload["key"],
        type_="plan_gate.resolved",
        payload={"action": decision.action, "effective_confidence": conf.effective},
    )
    out: dict[str, Any] = {"plan_decision": decision}
    if decision.action == "edit":
        assert decision.plan is not None
        problems = scheduler.validate_plan(decision.plan)
        if problems:  # defense in depth: the API rejects these with 422
            out["failure_reason"] = f"edited plan is invalid: {problems}"
            out["plan_decision"] = decision.model_copy(update={"action": "reject"})
        else:
            out["plan"] = decision.plan
    elif decision.action == "reject":
        out["failure_reason"] = decision.note or "plan rejected by reviewer"
    elif decision.action == "take_over":
        out["final"] = decision.deliverable
        out["outcome"] = Outcome(status="completed", reason="taken over by a human")
    return out


def after_plan_gate(state: TaskState) -> Literal["schedule", "finalize_rejected", "__end__"]:
    d = state.get("plan_decision")
    if d is None or d.action in {"approve", "edit"}:
        return "schedule"
    return "finalize_rejected" if d.action == "reject" else "__end__"
