"""Graph registry.

- `main`: the orchestration workflow (DECISIONS.md "Corrected workflow graph"). Phase 2
  builds this subset (ADR-026):

      START -> intake -> plan --(invalid, retry once)--> plan
                           |--(no valid plan / model failure)--> finalize_failed -> END
                           v
                        schedule --[Send]--> subtask_worker (subgraph) --> schedule
                           |-- escalate     -> escalation_gate
                           |                     |-- approve/edit -> schedule
                           |                     '-- reject       -> finalize_rejected -> END
                           |-- fail         -> finalize_failed -> END
                           |-- partial      -> synthesize -> final_review -> END
                           '-- all accepted -> synthesize -> END

  Not built yet, inserted later between existing nodes (no rewrite): recall (after intake),
  plan_gate (after plan), action_gate + execute_actions (before synthesize), consolidate
  (after synthesize). `final_review` exists but only scores partial deliverables (ADR-029).

  Every path that reaches END sets `outcome`. The worker treats a run that ends without one
  as `failed` (never silent).

- `demo`: Phase 1 two-node durability probe (step_one -> step_two).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from agentops.core.nodes.actions import action_gate, after_action_gate, execute_actions
from agentops.core.nodes.escalation import after_escalation, escalation_gate
from agentops.core.nodes.final_review import after_final_review, final_review
from agentops.core.nodes.finalize import finalize_failed, finalize_rejected
from agentops.core.nodes.intake import intake
from agentops.core.nodes.plan import after_plan, plan
from agentops.core.nodes.plan_gate import after_plan_gate, plan_gate
from agentops.core.nodes.schedule import route_schedule, schedule
from agentops.core.nodes.subtask import build_subtask_graph
from agentops.core.nodes.synthesize import after_synthesize, synthesize
from agentops.core.state import DemoState, Outcome, RunContext, TaskState
from agentops.core.testhooks import TEST_HOOK_ENV, block_if_hooked
from agentops.observability.events import emit, node_key

__all__ = ["GRAPHS", "TEST_HOOK_ENV", "GraphSpec", "build_demo_graph", "build_main_graph"]

AnyGraph = CompiledStateGraph[Any, RunContext, Any, Any]


# ---- main ----------------------------------------------------------------------------------
def build_main_graph(checkpointer: BaseCheckpointSaver[Any] | None) -> AnyGraph:
    g = StateGraph(TaskState, context_schema=RunContext)
    g.add_node("intake", intake)
    g.add_node("plan", plan)
    g.add_node("plan_gate", plan_gate)
    g.add_node("schedule", schedule)
    g.add_node("subtask_worker", build_subtask_graph())
    g.add_node("escalation_gate", escalation_gate)
    g.add_node("action_gate", action_gate)
    g.add_node("execute_actions", execute_actions)
    g.add_node("synthesize", synthesize)
    g.add_node("final_review", final_review)
    g.add_node("finalize_failed", finalize_failed)
    g.add_node("finalize_rejected", finalize_rejected)

    g.add_edge(START, "intake")
    g.add_edge("intake", "plan")
    g.add_conditional_edges("plan", after_plan, ["plan", "plan_gate", "finalize_failed"])
    g.add_conditional_edges("plan_gate", after_plan_gate, ["schedule", "finalize_rejected", END])
    g.add_conditional_edges(
        "schedule",
        route_schedule,
        ["subtask_worker", "escalation_gate", "finalize_failed", "action_gate"],
    )
    g.add_conditional_edges("action_gate", after_action_gate, ["action_gate", "execute_actions"])
    g.add_edge("execute_actions", "synthesize")
    g.add_edge("subtask_worker", "schedule")
    g.add_conditional_edges(
        "escalation_gate", after_escalation, ["schedule", "finalize_rejected", END]
    )
    g.add_conditional_edges("synthesize", after_synthesize, ["final_review", END])
    g.add_conditional_edges("final_review", after_final_review, ["escalation_gate", END])
    g.add_edge("finalize_failed", END)
    g.add_edge("finalize_rejected", END)
    return g.compile(checkpointer=checkpointer)


def main_input(task_id: str, request: str) -> dict[str, Any]:
    return {"task_id": task_id, "request": request}


def main_result(values: Mapping[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    outcome: Outcome | None = values.get("outcome")
    if outcome is None:
        return "failed", {}, "graph ended without an outcome"
    plan = values.get("plan")
    results = values.get("results") or {}
    result = {
        "final": values.get("final"),
        "plan": plan.model_dump(mode="json") if plan is not None else None,
        "subtasks": {
            sid: r.model_dump(mode="json", exclude={"output"}) for sid, r in results.items()
        },
        "partial": bool(values.get("partial")),
        # never executed in Phase 3; approval + execution is Phase 4 (ADR-003)
        "proposed_actions": [
            a.model_dump(mode="json") for a in (values.get("proposed_actions") or {}).values()
        ],
        "final_review": (
            values["final_review"].model_dump(mode="json") if values.get("final_review") else None
        ),
    }
    return outcome.status, result, outcome.reason if outcome.status != "completed" else None


# ---- demo (Phase 1 durability probe) -------------------------------------------------------
async def step_one(state: DemoState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await emit(
        ctx.engine,
        ctx.task_id,
        "node.completed",
        node="step_one",
        payload={"pid": os.getpid()},
        dedupe_key=node_key("step_one", "completed"),
    )
    return {"log": [f"step_one:{os.getpid()}"]}


async def step_two(state: DemoState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await emit(
        ctx.engine,
        ctx.task_id,
        "node.started",
        node="step_two",
        payload={"pid": os.getpid()},
        dedupe_key=node_key("step_two", "started"),
    )
    await block_if_hooked(str(ctx.task_id), "step_two", always=True)
    await emit(
        ctx.engine,
        ctx.task_id,
        "node.completed",
        node="step_two",
        payload={"pid": os.getpid()},
        dedupe_key=node_key("step_two", "completed"),
    )
    return {"log": [f"step_two:{os.getpid()}"]}


def build_demo_graph(checkpointer: BaseCheckpointSaver[Any] | None) -> AnyGraph:
    g = StateGraph(DemoState, context_schema=RunContext)
    g.add_node("step_one", step_one)
    g.add_node("step_two", step_two)
    g.add_edge(START, "step_one")
    g.add_edge("step_one", "step_two")
    g.add_edge("step_two", END)
    return g.compile(checkpointer=checkpointer)


def demo_input(task_id: str, request: str) -> dict[str, Any]:
    return {"request": request, "log": []}


def demo_result(values: Mapping[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    return "completed", {"log": values.get("log", [])}, None


# ---- registry ------------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphSpec:
    build: Callable[[BaseCheckpointSaver[Any] | None], AnyGraph]
    initial_input: Callable[[str, str], dict[str, Any]]
    # final state values -> (terminal status, result json, error)
    result: Callable[[Mapping[str, Any]], tuple[str, dict[str, Any], str | None]]
    needs_model: bool


GRAPHS: dict[str, GraphSpec] = {
    "main": GraphSpec(build_main_graph, main_input, main_result, needs_model=True),
    "demo": GraphSpec(build_demo_graph, demo_input, demo_result, needs_model=False),
}
