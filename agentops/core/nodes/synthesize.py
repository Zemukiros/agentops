"""synthesize: compose the final deliverable from accepted outputs; outcome `completed`."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import Outcome, RunContext, TaskState
from agentops.models.client import ModelCallError


async def synthesize(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    plan = state.get("plan")
    assert plan is not None
    await node_event(runtime, "synthesize", "started")
    outputs = {sid: r.output or "" for sid, r in state.get("results", {}).items()}
    msgs = prompts.synth_messages(state["request"], plan, outputs)
    try:
        res = await model_of(runtime).invoke(
            "supervisor", msgs, task_id=runtime.context.task_id, purpose="synthesize"
        )
    except ModelCallError as exc:
        return {
            "failure_reason": f"synthesis failed: {exc}",
            "outcome": Outcome(status="failed", reason=f"synthesis failed: {exc}"),
        }
    final = res.message.text
    await node_event(runtime, "synthesize", "completed", payload={"chars": len(final)})
    return {"final": final, "outcome": Outcome(status="completed")}
