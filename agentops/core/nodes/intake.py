"""intake: normalize the request. (Budget is opened with the task in POST /tasks.)"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core.nodes.common import node_event
from agentops.core.state import RunContext, TaskState

MAX_REQUEST_CHARS = 20_000


async def intake(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    request = " ".join(state.get("request", "").split())[:MAX_REQUEST_CHARS]
    await node_event(runtime, "intake", "completed", payload={"chars": len(request)})
    return {
        "request": request,
        "plan": None,
        "plan_attempts": 0,
        "plan_rejections": [],
        "results": {},
        "reviews": [],
        "wave": 0,
        "decision": None,
        "escalations": [],
        "proposed_actions": {},
        "failure_reason": None,
        "final": None,
        "partial": False,
        "final_review": None,
        "outcome": None,
    }
