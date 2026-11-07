"""action_gate + execute_actions (ADR-003, ADR-004, ADR-037).

action_gate (serial): ProposedActions from ACCEPTED subtasks only (already deduped by hash).
Each pass asks about one undecided action and loops back; bounded by the number of actions.
Pure computation before interrupt().

execute_actions (serial; the only node with external side effects):
  approve / edit -> key = sha256(task, action hash, canonical final args)
                    INSERT side_effects ... ON CONFLICT DO NOTHING
                    inserted  -> perform (registry.execute_approved, key passed on) -> done/failed
                    existing done      -> reuse the stored result (never re-run)
                    existing failed    -> report failed (never re-run)
                    existing in_progress -> a previous attempt crashed between performing and
                                     marking done: outcome UNKNOWN. Never retried; a non-blocking
                                     side_effect_review inbox item is raised; the run ends
                                     completed_partial.
  reject  -> not executed;  take_over -> recorded as done by a human, not executed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from sqlalchemy import text

from agentops import approvals
from agentops.core.nodes.common import model_of, node_event
from agentops.core.state import (
    ActionDecision,
    ActionDecisionRecord,
    ActionOutcome,
    ProposedAction,
    RunContext,
    TaskState,
)
from agentops.core.testhooks import block_if_hooked
from agentops.tools.native.workspace import task_workspace
from agentops.tools.spec import ToolContext

FINAL_STATUSES = frozenset({"done", "failed", "unknown", "rejected", "human_done"})


def eligible_actions(state: TaskState) -> list[ProposedAction]:
    results = state.get("results", {})
    accepted = {sid for sid, r in results.items() if r.status == "accepted"}
    proposals = state.get("proposed_actions", {}) or {}
    return sorted((p for p in proposals.values() if p.subtask_id in accepted), key=lambda p: p.hash)


def _undecided(state: TaskState) -> list[ProposedAction]:
    decided = state.get("action_decisions", {}) or {}
    return [p for p in eligible_actions(state) if p.hash not in decided]


def action_payload(p: ProposedAction, input_schema: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "approve_action",
        "key": f"action:{p.hash}",
        "hash": p.hash,
        "tool": p.tool,
        "risk": p.risk,
        "args": p.args,
        "rationale": p.rationale,
        "agent": p.agent,
        "subtask_id": p.subtask_id,
        "input_schema": input_schema,
        "actions": ["approve", "edit", "reject", "take_over"],
    }


async def action_gate(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    pending = _undecided(state)
    if not pending:
        return {}
    p = pending[0]
    registry = runtime.context.tools
    spec = registry.specs.get(p.tool) if registry is not None else None
    schema = spec.input_model.model_json_schema() if spec is not None else None
    payload = action_payload(p, schema)  # pure
    await block_if_hooked(str(runtime.context.task_id), "action_gate")
    decision: ActionDecision = interrupt(payload, response_schema=ActionDecision)
    await block_if_hooked(str(runtime.context.task_id), "action_gate_resumed")
    await node_event(
        runtime,
        "action_gate",
        "resolved",
        p.hash[:16],
        type_="action.decided",
        payload={"hash": p.hash, "tool": p.tool, "action": decision.action},
    )
    return {
        "action_decisions": {
            p.hash: ActionDecisionRecord(hash=p.hash, key=payload["key"], decision=decision)
        }
    }


def after_action_gate(state: TaskState) -> Literal["action_gate", "execute_actions"]:
    return "action_gate" if _undecided(state) else "execute_actions"


def idempotency_key(task_id: str, action_hash: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{task_id}:{action_hash}:{canonical}".encode()).hexdigest()


async def execute_actions(state: TaskState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    await block_if_hooked(str(runtime.context.task_id), "execute_actions")
    proposals = state.get("proposed_actions", {}) or {}
    decisions = state.get("action_decisions", {}) or {}
    done_before = state.get("action_outcomes", {}) or {}
    outcomes: dict[str, ActionOutcome] = {}
    accepted_hashes = {p.hash for p in eligible_actions(state)}
    for h in sorted(proposals):
        if h not in accepted_hashes:
            outcomes[h] = ActionOutcome(
                hash=h,
                tool=proposals[h].tool,
                status="not_considered",
                detail="proposed by a subtask that was not accepted",
            )
    for h in sorted(decisions):
        if h in done_before and done_before[h].status in FINAL_STATUSES:
            continue
        rec, p = decisions[h], proposals[h]
        d = rec.decision
        if d.action == "reject":
            outcomes[h] = ActionOutcome(hash=h, tool=p.tool, status="rejected", detail=d.note)
            continue
        if d.action == "take_over":
            outcomes[h] = ActionOutcome(
                hash=h, tool=p.tool, status="human_done", detail=d.note or "done by a human"
            )
            continue
        args = d.args if d.action == "edit" and d.args is not None else p.args
        outcomes[h] = await _execute_one(runtime, state, p, args)
    for h, o in outcomes.items():
        await node_event(
            runtime,
            "execute_actions",
            "outcome",
            h[:16],
            type_="action.outcome",
            payload=o.model_dump(),
        )
    return {"action_outcomes": outcomes} if outcomes else {}


async def _execute_one(
    runtime: Runtime[RunContext], state: TaskState, p: ProposedAction, args: dict[str, Any]
) -> ActionOutcome:
    ctx_run = runtime.context
    task_id = ctx_run.task_id
    key = idempotency_key(str(task_id), p.hash, args)
    engine = ctx_run.engine
    async with engine.begin() as c:
        inserted = (
            await c.execute(
                text(
                    "INSERT INTO side_effects (idempotency_key, task_id, action_hash, status, "
                    "tool, args) VALUES (:k, :t, :h, 'in_progress', :tool, CAST(:a AS jsonb)) "
                    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key"
                ),
                {"k": key, "t": task_id, "h": p.hash, "tool": p.tool, "a": json.dumps(args)},
            )
        ).first()
    if inserted is None:
        return await _existing(runtime, p, key)

    registry = ctx_run.tools
    assert registry is not None
    settings = model_of(runtime).settings
    tool_ctx = ToolContext(
        task_id=task_id,
        owner_id=ctx_run.owner_id,
        agent="execute_actions",
        subtask_id=p.subtask_id,
        revision=p.revision,
        step=0,
        workspace=task_workspace(settings, task_id),
        settings=settings,
        engine=engine,
    )
    await block_if_hooked(str(task_id), "execute_before_perform")
    try:
        out = await registry.execute_approved(p.tool, args, tool_ctx, idempotency_key=key)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        async with engine.begin() as c:
            await c.execute(
                text(
                    "UPDATE side_effects SET status = 'failed', error = :e, updated_at = now(), "
                    "completed_at = now() WHERE idempotency_key = :k"
                ),
                {"k": key, "e": detail},
            )
        return ActionOutcome(
            hash=p.hash, tool=p.tool, status="failed", idempotency_key=key, detail=detail
        )
    await block_if_hooked(str(task_id), "execute_after_perform")
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE side_effects SET status = 'done', result = CAST(:r AS jsonb), "
                "updated_at = now(), completed_at = now() WHERE idempotency_key = :k"
            ),
            {"k": key, "r": json.dumps({"text": out.text[:2000], "data": out.data}, default=str)},
        )
    return ActionOutcome(
        hash=p.hash, tool=p.tool, status="done", idempotency_key=key, detail=out.text[:300]
    )


async def _existing(runtime: Runtime[RunContext], p: ProposedAction, key: str) -> ActionOutcome:
    """A row with this key already exists: never perform again."""
    engine = runtime.context.engine
    async with engine.connect() as c:
        status, result, error = (
            await c.execute(
                text(
                    "SELECT status::text, result, error FROM side_effects WHERE idempotency_key=:k"
                ),
                {"k": key},
            )
        ).one()
    if status == "done":
        detail = (result or {}).get("text") if isinstance(result, dict) else None
        return ActionOutcome(
            hash=p.hash,
            tool=p.tool,
            status="done",
            idempotency_key=key,
            detail=(detail or "done earlier")[:300],
        )
    if status == "failed":
        return ActionOutcome(
            hash=p.hash, tool=p.tool, status="failed", idempotency_key=key, detail=error
        )
    # in_progress: performed or not, we can't know. Surface it; never retry (ADR-004).
    async with engine.begin() as c:
        await approvals.record_pending(
            c,
            runtime.context.task_id,
            kind="side_effect_review",
            key=f"side_effect:{key}",
            payload={
                "idempotency_key": key,
                "tool": p.tool,
                "args": p.args,
                "hash": p.hash,
                "question": "A worker stopped after starting this action and before recording "
                "its result. Did it happen? Resolving this only updates records; nothing is "
                "re-executed.",
                "actions": ["confirmed_done", "not_done"],
            },
            settings=model_of(runtime).settings,
            blocking=False,
        )
    return ActionOutcome(
        hash=p.hash,
        tool=p.tool,
        status="unknown",
        idempotency_key=key,
        detail="outcome unknown after a crash; flagged for human review",
    )
