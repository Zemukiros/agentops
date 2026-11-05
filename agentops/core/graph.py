"""Graph registry.

Phase 1 ships one graph: `demo`, a two-node durability probe (step_one -> step_two). It exists
to prove, before the real workflow is built, that a run checkpointed by one worker process
continues on another process after the first dies.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from agentops.core.state import DemoState, RunContext
from agentops.observability.events import emit, node_key

# Test-only hook: when set, step_two writes a marker file and blocks until a release file
# exists, so an integration test can kill the worker deterministically inside step_two.
TEST_HOOK_ENV = "AGENTOPS_TEST_HOOK_DIR"
_HOOK_TIMEOUT_S = 60.0


async def _wait_for_release(task_id: str) -> None:
    hook_dir = os.environ.get(TEST_HOOK_ENV)
    if not hook_dir:
        return
    d = Path(hook_dir)
    (d / f"{task_id}.step_two.started").write_text(str(os.getpid()))
    deadline = time.monotonic() + _HOOK_TIMEOUT_S
    while not (d / f"{task_id}.release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test hook release file never appeared")
        await asyncio.sleep(0.05)


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
    await _wait_for_release(str(ctx.task_id))
    await emit(
        ctx.engine,
        ctx.task_id,
        "node.completed",
        node="step_two",
        payload={"pid": os.getpid()},
        dedupe_key=node_key("step_two", "completed"),
    )
    return {"log": [f"step_two:{os.getpid()}"]}


def build_demo_graph(
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[DemoState, RunContext, DemoState, DemoState]:
    g = StateGraph(DemoState, context_schema=RunContext)
    g.add_node("step_one", step_one)
    g.add_node("step_two", step_two)
    g.add_edge(START, "step_one")
    g.add_edge("step_one", "step_two")
    g.add_edge("step_two", END)
    return g.compile(checkpointer=checkpointer)


GRAPHS = {"demo": build_demo_graph}
