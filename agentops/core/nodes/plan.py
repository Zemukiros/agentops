"""plan: supervisor produces a structured Plan; DAG + specialist validation; one re-plan.

Cycle bound (DECISIONS invariant): at most MAX_PLAN_ATTEMPTS model plans, counted in state.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime

from agentops.core import prompts, scheduler
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import Plan, PlanRejection, RunContext, TaskState
from agentops.models.client import ModelCallError

MAX_PLAN_ATTEMPTS = 2  # first plan + one re-plan


def _tool_names(runtime: Runtime[RunContext]) -> dict[str, list[str]] | None:
    reg = runtime.context.tools
    if reg is None:
        return None
    return {sp: [t.name for t in reg.tools_for(sp)] for sp in scheduler.ENABLED_SPECIALISTS}


async def plan(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    attempt = state.get("plan_attempts", 0) + 1
    await node_event(runtime, "plan", "started", f"p{attempt}")
    previous = [(r.plan_json, r.errors) for r in state.get("plan_rejections", [])]
    msgs = prompts.planner_messages(
        state["request"],
        specialists=sorted(scheduler.ENABLED_SPECIALISTS),
        max_subtasks=scheduler.MAX_SUBTASKS,
        previous=previous,
        memories=state.get("memories") or [],
        tools=_tool_names(runtime),
    )
    try:
        res = await model_of(runtime).invoke(
            "supervisor",
            msgs,
            task_id=runtime.context.task_id,
            purpose=f"plan/p{attempt}",
            schema=Plan,
        )
    except ModelCallError as exc:
        return {"plan_attempts": attempt, "failure_reason": f"planning failed: {exc}"}
    assert isinstance(res.parsed, Plan)
    candidate = res.parsed
    errors = scheduler.validate_plan(candidate)
    if errors:
        await node_event(
            runtime,
            "plan",
            "invalid",
            f"p{attempt}",
            type_="plan.invalid",
            payload={"attempt": attempt, "errors": errors, "plan": candidate.model_dump()},
        )
        out: dict[str, Any] = {
            "plan_attempts": attempt,
            "plan_rejections": [
                *state.get("plan_rejections", []),
                PlanRejection(
                    attempt=attempt, plan_json=prompts.plan_json(candidate), errors=errors
                ),
            ],
            "plan": None,
        }
        if attempt >= MAX_PLAN_ATTEMPTS:
            out["failure_reason"] = f"no valid plan after {attempt} attempts: {errors}"
        return out
    await node_event(
        runtime,
        "plan",
        "created",
        f"p{attempt}",
        type_="plan.created",
        payload={"attempt": attempt, "plan": candidate.model_dump()},
    )
    confidence = scheduler.plan_confidence(
        candidate,
        memory_similarity=state.get("memory_similarity") or 0.0,
        threshold=model_of(runtime).settings.plan_confidence_threshold,
    )
    # Computed (and evented) here, so plan_gate does only pure work before interrupt().
    await node_event(
        runtime,
        "plan",
        "confidence",
        f"p{attempt}",
        type_="plan.confidence",
        payload={**confidence.model_dump(), "needs_approval": confidence.needs_approval},
    )
    return {"plan_attempts": attempt, "plan": candidate, "plan_confidence": confidence}


def after_plan(state: TaskState) -> Literal["plan", "plan_gate", "finalize_failed"]:
    if state.get("plan") is not None:
        return "plan_gate"
    if state.get("failure_reason") or state.get("plan_attempts", 0) >= MAX_PLAN_ATTEMPTS:
        return "finalize_failed"
    return "plan"
