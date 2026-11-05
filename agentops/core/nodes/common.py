"""Helpers shared by graph nodes."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core.state import RunContext
from agentops.models.client import ModelClient
from agentops.observability.events import emit, node_key


async def node_event(
    runtime: Runtime[RunContext],
    node: str,
    event: str,
    *coords: object,
    type_: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit an idempotent node event (ADR-025). `coords` must come from checkpointed state."""
    ctx = runtime.context
    await emit(
        ctx.engine,
        ctx.task_id,
        type_ or f"node.{event}",
        node=node,
        payload=payload,
        dedupe_key=node_key(node, event, *coords),
    )


def model_of(runtime: Runtime[RunContext]) -> ModelClient:
    model = runtime.context.model
    if model is None:
        raise RuntimeError("this graph needs a ModelClient in RunContext")
    return model
