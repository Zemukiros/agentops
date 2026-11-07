"""escalation_gate: serial node; pauses the run for a human decision (ADR-003, ADR-026).

Only pure computation happens before interrupt(), because LangGraph re-runs the node from its
first line on resume (verified, ADR-003). The `escalation.requested` event is emitted by
`schedule`, not here.

Resume value (validated): EscalationDecision
  approve -> the needs_human subtasks are accepted as-is (human_override)
  edit    -> the given outputs replace the drafts, then accepted (human_override)
  reject  -> the run ends `rejected`
The approvals table, API and inbox are Phase 4; until then a `resume` job carries the value.

Cycle bound: each round resolves every needs_human subtask, so rounds <= number of subtasks.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agentops.core.nodes.common import node_event
from agentops.core.nodes.synthesize import delivery_outcome
from agentops.core.state import (
    Escalation,
    EscalationDecision,
    FinalDecision,
    Outcome,
    RunContext,
    SubtaskResult,
    TaskState,
)
from agentops.core.testhooks import block_if_hooked


def escalation_payload(state: TaskState) -> dict[str, Any]:
    decision = state.get("decision")
    assert decision is not None and decision.kind == "escalate"
    results = state.get("results", {})
    reviews = state.get("reviews", [])
    rnd = len(state.get("escalations", [])) + 1
    return {
        "kind": "escalation",
        "key": f"escalation:e{rnd}",
        "round": rnd,
        "subtasks": [
            {
                "subtask_id": sid,
                "reason": results[sid].reason,
                "draft": results[sid].output,
                "reviews": [r.verdict.model_dump() for r in reviews if r.subtask_id == sid],
            }
            for sid in decision.subtask_ids
        ],
        "actions": ["approve", "edit", "reject", "take_over"],
    }


def final_payload(state: TaskState) -> dict[str, Any]:
    n = len(state.get("final_decisions", [])) + 1
    verdict = state.get("final_review")
    return {
        "kind": "final_review",
        "key": f"final:f{n}",
        "request": state["request"],
        "deliverable": state.get("final"),
        "partial": bool(state.get("partial")),
        "review": verdict.model_dump() if verdict else None,
        "actions": ["approve", "edit", "reject", "take_over"],
    }


async def final_gate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """escalation_gate in final-review mode: the deliverable scored below threshold."""
    payload = final_payload(state)  # pure
    await block_if_hooked(str(runtime.context.task_id), "escalation_gate")
    decision: FinalDecision = interrupt(payload, response_schema=FinalDecision)
    await block_if_hooked(str(runtime.context.task_id), "escalation_gate_resumed")
    score = payload["review"]["score"] if payload["review"] else None
    await node_event(
        runtime,
        "escalation_gate",
        "final_resolved",
        payload["key"],
        type_="escalation.resolved",
        payload={"kind": "final_review", "action": decision.action, "score": score},
    )
    out: dict[str, Any] = {
        "final_decisions": [decision],
        "final_review_failed": False,
        "last_gate_action": decision.action,
    }
    if decision.action == "reject":
        out["failure_reason"] = decision.note or "deliverable rejected by reviewer"
    elif decision.action == "take_over":
        out["final"] = decision.deliverable
        out["outcome"] = Outcome(status="completed", reason="taken over by a human")
    else:
        if decision.action == "edit":
            out["final"] = decision.deliverable
        note = (
            f"deliverable {'edited' if decision.action == 'edit' else 'accepted'} by a human "
            f"after a final review score of {score}/5"
        )
        out["outcome"] = delivery_outcome(state, note=note)
    return out


async def escalation_gate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    if state.get("final_review_failed"):
        return await final_gate(state, runtime)
    payload = escalation_payload(state)  # pure
    await block_if_hooked(str(runtime.context.task_id), "escalation_gate")
    raw = interrupt(payload, response_schema=EscalationDecision)
    await block_if_hooked(str(runtime.context.task_id), "escalation_gate_resumed")
    decision = EscalationDecision.model_validate(raw)
    ids: list[str] = [s["subtask_id"] for s in payload["subtasks"]]
    unknown = sorted(set(decision.edits) - set(ids))
    if unknown:
        raise ValueError(f"edits for subtasks not under escalation: {unknown}")
    rnd = payload["round"]
    await node_event(
        runtime,
        "escalation_gate",
        "resolved",
        f"e{rnd}",
        type_="escalation.resolved",
        payload={"round": rnd, "action": decision.action, "subtask_ids": ids},
    )
    out: dict[str, Any] = {
        "escalations": [Escalation(round=rnd, subtask_ids=ids, decision=decision)],
        "last_gate_action": decision.action,
    }
    if decision.action == "reject":
        out["failure_reason"] = decision.note or "rejected by reviewer"
        return out
    if decision.action == "take_over":
        # A human supplies the whole deliverable; nothing else runs (ADR-035).
        out["final"] = decision.deliverable
        out["outcome"] = Outcome(status="completed", reason="taken over by a human")
        return out
    results = state.get("results", {})
    out["results"] = {
        sid: SubtaskResult(
            subtask_id=sid,
            status="accepted",
            output=decision.edits.get(sid, results[sid].output),
            revisions=results[sid].revisions,
            reason=f"human {decision.action}",
            human_override=True,
        )
        for sid in ids
    }
    return out


def after_escalation(state: TaskState) -> Literal["schedule", "finalize_rejected", "consolidate"]:
    if state.get("last_gate_action") == "reject":
        return "finalize_rejected"
    if state.get("outcome") is not None:
        return "consolidate"
    return "schedule"
