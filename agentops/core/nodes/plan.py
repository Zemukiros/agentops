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


async def plan(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    attempt = state.get("plan_attempts", 0) + 1
    await node_event(runtime, "plan", "started", f"p{attempt}")
    previous = [(r.plan_json, r.errors) for r in state.get("plan_rejections", [])]
    msgs = prompts.planner_messages(
        state["request"],
        specialists=sorted(scheduler.ENABLED_SPECIALISTS),
        max_subtasks=scheduler.MAX_SUBTASKS,
        previous=previous,
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
    return {"plan_attempts": attempt, "plan": candidate}


def after_plan(state: TaskState) -> Literal["plan", "schedule", "finalize_failed"]:
    if state.get("plan") is not None:
        return "schedule"
    if state.get("failure_reason") or state.get("plan_attempts", 0) >= MAX_PLAN_ATTEMPTS:
        return "finalize_failed"
    return "plan"
