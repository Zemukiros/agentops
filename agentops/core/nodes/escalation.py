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
from agentops.core.state import (
    Escalation,
    EscalationDecision,
    RunContext,
    SubtaskResult,
    TaskState,
)


def escalation_payload(state: TaskState) -> dict[str, Any]:
    decision = state.get("decision")
    assert decision is not None and decision.kind == "escalate"
    results = state.get("results", {})
    reviews = state.get("reviews", [])
    return {
        "kind": "escalation",
        "round": len(state.get("escalations", [])) + 1,
        "subtasks": [
            {
                "subtask_id": sid,
                "reason": results[sid].reason,
                "draft": results[sid].output,
                "reviews": [r.verdict.model_dump() for r in reviews if r.subtask_id == sid],
            }
            for sid in decision.subtask_ids
        ],
        "actions": ["approve", "edit", "reject"],
    }


async def escalation_gate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    payload = escalation_payload(state)  # pure
    raw = interrupt(payload, response_schema=EscalationDecision)
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
        "escalations": [Escalation(round=rnd, subtask_ids=ids, decision=decision)]
    }
    if decision.action == "reject":
        out["failure_reason"] = decision.note or "rejected by reviewer"
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


def after_escalation(state: TaskState) -> Literal["schedule", "finalize_rejected"]:
    last = state["escalations"][-1]
    return "finalize_rejected" if last.decision.action == "reject" else "schedule"
