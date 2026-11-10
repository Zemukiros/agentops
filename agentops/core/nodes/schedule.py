"""schedule: serial node that turns terminal results into the next step (ADR-002).

The node computes the decision (pure, `scheduler.decide`) and stores it in state; the
conditional edge only reads it. Dispatching increments `wave`, so the loop is bounded by
`scheduler.max_waves` (DECISIONS invariant: counter-bounded cycles).
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import Send

from agentops.core import replay, scheduler
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import RunContext, SubtaskInput, SubtaskResult, TaskState


async def schedule(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None, "schedule runs only after a valid plan"
    results = state.get("results", {})
    wave = state.get("wave", 0)
    # Wave boundary: the workspace now holds exactly what waves 1..wave wrote. Keep a copy so a
    # replay branch forked here starts from the same files (ADR-040). Internal and idempotent:
    # a re-run of this pass copies the same files again; nothing outside the task changes.
    try:
        await asyncio.to_thread(
            replay.snapshot_workspace, model_of(runtime).settings, runtime.context.task_id, wave
        )
    except OSError as exc:
        # A snapshot only serves future replays: never fail the task over it (ADR-046). A fork
        # at this wave then fails with "snapshot missing" instead of guessing.
        await node_event(
            runtime,
            "schedule",
            "snapshot_failed",
            f"w{wave}",
            type_="replay.snapshot_failed",
            payload={"wave": wave, "error": f"{type(exc).__name__}: {exc}"[:300]},
        )
    decision = scheduler.decide(plan, results, wave)
    rounds = len(state.get("escalations", []))
    out: dict[str, Any] = {"decision": decision}
    if decision.blocked:
        out["results"] = {
            sid: SubtaskResult(subtask_id=sid, status="blocked", reason=reason)
            for sid, reason in decision.blocked.items()
        }
        for sid, reason in sorted(decision.blocked.items()):
            await node_event(
                runtime,
                "schedule",
                "blocked",
                sid,
                type_="subtask.blocked",
                payload={"subtask_id": sid, "reason": reason},
            )
    if decision.kind == "dispatch":
        wave += 1
        out["wave"] = wave
        for sid in decision.subtask_ids:
            await node_event(
                runtime,
                "schedule",
                "dispatched",
                f"w{wave}",
                sid,
                type_="subtask.dispatched",
                payload={"subtask_id": sid, "wave": wave},
            )
    elif decision.kind == "escalate":
        # Emitted here, not in escalation_gate: the gate must do only pure work before
        # interrupt() (DECISIONS invariant).
        await node_event(
            runtime,
            "schedule",
            "escalation",
            f"e{rounds + 1}",
            type_="escalation.requested",
            payload={"subtask_ids": decision.subtask_ids, "round": rounds + 1},
        )
    elif decision.kind == "fail":
        out["failure_reason"] = decision.reason
    await node_event(
        runtime,
        "schedule",
        "decided",
        f"w{wave}",
        f"e{rounds}",
        len(results),
        type_="schedule.decided",
        payload=decision.model_dump(),
    )
    return out


def route_schedule(state: TaskState) -> list[Send] | str:
    decision = state.get("decision")
    plan = state.get("plan")
    assert decision is not None and plan is not None
    if decision.kind == "dispatch":
        results = state.get("results", {})
        by_id = {s.id: s for s in plan.subtasks}
        sends = []
        for sid in decision.subtask_ids:
            sub = by_id[sid]
            payload: SubtaskInput = {
                "task_id": state["task_id"],
                "request": state["request"],
                "subtask": sub,
                "context": {d: results[d].output or "" for d in sub.depends_on},
                "wave": state.get("wave", 0),
            }
            sends.append(Send("subtask_worker", payload))
        return sends
    return {
        "escalate": "escalation_gate",
        "fail": "finalize_failed",
        "all_accepted": "action_gate",
        "partial": "action_gate",
    }[decision.kind]
