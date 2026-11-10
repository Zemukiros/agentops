"""recall (after intake) and consolidate (before every END) - ADR-038."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from agentops.core import prompts
from agentops.core.budget import BudgetExceededError
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import MemoryHit, RunContext, TaskState
from agentops.memory import store
from agentops.memory.service import MemoryCandidates
from agentops.models.client import ModelCallError


async def recall(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    if ctx.memory is None or ctx.owner_id is None:
        return {"memories": [], "memory_similarity": 0.0}
    rows = await ctx.memory.recall(ctx.owner_id, state["request"])
    hits = [
        MemoryHit(
            id=str(r.id),
            kind=r.kind,
            text=r.text,
            outcome=r.outcome,
            similarity=round(r.similarity or 0.0, 4),
            score=round(r.score or 0.0, 4),
        )
        for r in rows
    ]
    await store.touch(ctx.engine, [r.id for r in rows])
    await node_event(
        runtime,
        "recall",
        "completed",
        type_="memory.recalled",
        payload={"count": len(hits), "memories": [h.model_dump() for h in hits]},
    )
    return {"memories": hits, "memory_similarity": max((h.similarity for h in hits), default=0.0)}


async def consolidate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Never changes the outcome and never fails the run (Phase 4 change 2)."""
    ctx = runtime.context
    outcome = state.get("outcome")
    if ctx.memory is None or ctx.owner_id is None or outcome is None:
        return {"memories_written": 0}
    if ctx.replay is not None:
        # A replay branch is an experiment, not experience: it writes no memories (ADR-040).
        await node_event(
            runtime,
            "consolidate",
            "skipped",
            type_="memory.consolidated",
            payload={"written": 0, "llm_skipped": "replay branch: no memories written"},
        )
        return {"memories_written": 0}
    results = state.get("results", {}) or {}
    accepted = {sid: r.output or "" for sid, r in results.items() if r.status == "accepted"}
    notes = [
        f"{e.decision.action}: {e.decision.note}"
        for e in state.get("escalations", [])
        if e.decision.note
    ] + [f"final {d.action}: {d.note}" for d in state.get("final_decisions", []) if d.note]
    candidates = []
    skipped = None
    try:
        res = await model_of(runtime).invoke(
            "supervisor",
            prompts.consolidate_messages(
                state["request"], outcome.status, outcome.reason, accepted, notes
            ),
            task_id=ctx.task_id,
            purpose="consolidate",
            schema=MemoryCandidates,
        )
        assert isinstance(res.parsed, MemoryCandidates)
        candidates = list(res.parsed.memories)
    except BudgetExceededError as exc:
        skipped = f"budget: {exc.reason}"
    except ModelCallError as exc:
        skipped = f"model: {str(exc)[:200]}"
    written = await ctx.memory.consolidate(
        task_id=ctx.task_id,
        user_id=ctx.owner_id,
        request=state["request"],
        status=outcome.status,
        reason=outcome.reason,
        candidates=candidates,
    )
    await node_event(
        runtime,
        "consolidate",
        "completed",
        type_="memory.consolidated",
        payload={
            "written": len(written.ids),
            "dropped": [{"text": t, "why": w} for t, w in written.dropped],
            "llm_skipped": skipped,
        },
    )
    return {"memories_written": len(written.ids)}
