"""Terminal nodes for the unhappy paths. Each sets exactly one Outcome (DECISIONS invariant)."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core.nodes.common import node_event
from agentops.core.state import Outcome, RunContext, TaskState


async def finalize_failed(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    reason = state.get("failure_reason") or "failed"
    await node_event(runtime, "finalize_failed", "completed", payload={"reason": reason})
    return {"outcome": Outcome(status="failed", reason=reason)}


async def finalize_rejected(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    reason = state.get("failure_reason") or "rejected"
    await node_event(runtime, "finalize_rejected", "completed", payload={"reason": reason})
    return {"outcome": Outcome(status="rejected", reason=reason)}
