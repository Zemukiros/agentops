"""Approvals: the human-decision inbox (ADR-035).

- `record_pending` creates one row per decision point. The key is semantic ("plan:p1",
  "action:<hash>", "escalation:e1", "final:f1", "budget:b1", "recursion:x1",
  "side_effect:<key>"), so a node re-run after a crash can't create a second row.
- `decide` is the ONLY way a pending row changes: a version-checked UPDATE. The API and the
  expiry sweep both use it, so two concurrent decisions (two reviewers, or a reviewer and the
  expiry) -> exactly one wins; the other sees `None` (HTTP 409 in the API).
- `apply` acts on a won decision: graph kinds enqueue a `resume` job with the decision as the
  interrupt's resume value; worker kinds (budget, recursion) enqueue a `resume` job for the
  worker; `side_effect_review` only updates the side_effects row (never re-executes).
- `validate_decision` runs in the API before anything is written: the same Pydantic models the
  gates use with interrupt(response_schema=...), plus semantic checks (DAG for an edited plan,
  the tool's JSON schema for edited args, the raise ceiling for budgets).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jsonschema
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentops.core import scheduler
from agentops.core.state import (
    ActionDecision,
    BudgetDecision,
    EscalationDecision,
    FinalDecision,
    PlanDecision,
    RecursionDecision,
    SideEffectResolution,
)
from agentops.observability.events import emit_on
from agentops.queue.jobs import enqueue_on
from agentops.settings import Settings

DECISION_MODELS: dict[str, type[BaseModel]] = {
    "approve_plan": PlanDecision,
    "approve_action": ActionDecision,
    "escalation": EscalationDecision,
    "final_review": FinalDecision,
    "budget": BudgetDecision,
    "recursion": RecursionDecision,
    "side_effect_review": SideEffectResolution,
}
WORKER_KINDS = frozenset({"budget", "recursion"})
STATUS_FOR_ACTION = {
    "approve": "approved",
    "edit": "edited",
    "reject": "rejected",
    "take_over": "taken_over",
    "confirmed_done": "approved",
    "not_done": "rejected",
}
EXPIRED_DECISION = {"action": "reject", "note": "approval expired"}


class DecisionError(ValueError):
    """The decision is invalid for this approval (HTTP 422)."""


@dataclass(frozen=True)
class Decided:
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    key: str
    source: str
    blocking: bool
    payload: dict[str, Any]
    version: int


def response_schema(kind: str) -> dict[str, Any]:
    return DECISION_MODELS[kind].model_json_schema()


async def record_pending(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    *,
    kind: str,
    key: str,
    payload: dict[str, Any],
    settings: Settings,
    source: str = "graph",
    blocking: bool = True,
) -> uuid.UUID | None:
    """Insert the approval if this key is new. Returns its id, or None if it already existed."""
    if kind not in DECISION_MODELS:
        raise ValueError(f"unknown approval kind {kind!r}")
    ttl = timedelta(seconds=settings.approval_ttl_seconds if blocking else 3650 * 86400)
    row = (
        await conn.execute(
            text(
                "INSERT INTO approvals (id, task_id, level, key, payload, source, blocking, "
                "expires_at) VALUES (:id, :t, :kind, :key, CAST(:p AS jsonb), :src, :blk, "
                "now() + :ttl) ON CONFLICT (task_id, key) DO NOTHING RETURNING id"
            ),
            {
                "id": uuid.uuid4(),
                "t": task_id,
                "kind": kind,
                "key": key,
                "p": json.dumps({**payload, "response_schema": response_schema(kind)}, default=str),
                "src": source,
                "blk": blocking,
                "ttl": ttl,
            },
        )
    ).first()
    if row is None:
        return None
    await emit_on(
        conn,
        task_id,
        "approval.requested",
        payload={"approval_id": str(row[0]), "kind": kind, "key": key, "blocking": blocking},
        dedupe_key=f"approval:{key}"[:200],
    )
    return uuid.UUID(str(row[0]))


def validate_decision(
    kind: str, raw: dict[str, Any], approval_payload: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Return the normalized decision, or raise DecisionError (HTTP 422)."""
    model = DECISION_MODELS.get(kind)
    if model is None:
        raise DecisionError(f"unknown approval kind {kind!r}")
    try:
        decision = model.model_validate(raw)
    except ValidationError as exc:
        raise DecisionError(_errors(exc)) from None
    check = _SEMANTIC_CHECKS.get(type(decision))
    if check is not None:
        check(decision, approval_payload, settings)
    return decision.model_dump(mode="json")


def _check_plan(d: Any, payload: dict[str, Any], settings: Settings) -> None:
    if d.action == "edit":
        problems = scheduler.validate_plan(d.plan)
        if problems:
            raise DecisionError(f"edited plan is invalid: {problems}")


def _check_action(d: Any, payload: dict[str, Any], settings: Settings) -> None:
    if d.action != "edit":
        return
    schema = payload.get("input_schema")
    if not isinstance(schema, dict):
        raise DecisionError("this action can't be edited (no input schema recorded)")
    try:
        jsonschema.validate(d.args, schema)
    except jsonschema.ValidationError as exc:
        raise DecisionError(f"edited args are invalid: {exc.message}") from None


def _check_escalation(d: Any, payload: dict[str, Any], settings: Settings) -> None:
    if d.action == "edit":
        allowed = {s["subtask_id"] for s in payload.get("subtasks", [])}
        unknown = sorted(set(d.edits) - allowed)
        if unknown:
            raise DecisionError(f"edits for subtasks not under escalation: {unknown}")


def _check_budget(d: Any, payload: dict[str, Any], settings: Settings) -> None:
    if d.action != "approve":
        return
    for field in ("max_usd", "max_llm_calls", "max_tokens"):
        new = getattr(d, field)
        if new is not None and new <= float(payload.get(field, 0)):
            raise DecisionError(f"{field} must be above the current limit")
    if d.max_usd is not None and d.max_usd > settings.budget_max_raise_usd:
        raise DecisionError(
            f"max_usd can't exceed BUDGET_MAX_RAISE_USD (${settings.budget_max_raise_usd:.2f})"
        )


_SEMANTIC_CHECKS: dict[type[BaseModel], Any] = {
    PlanDecision: _check_plan,
    ActionDecision: _check_action,
    EscalationDecision: _check_escalation,
    BudgetDecision: _check_budget,
}


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
        for e in exc.errors(include_url=False)[:8]
    )


_DECIDE = text(
    """
    UPDATE approvals SET status = CAST(:status AS approval_status), decision = CAST(:d AS jsonb),
        decided_by = :by, decided_at = now(), version = version + 1
    WHERE id = :id AND version = :v AND status = 'pending'
    RETURNING id, task_id, level, key, source, blocking, payload, version
    """
)


async def decide(
    conn: AsyncConnection,
    approval_id: uuid.UUID,
    *,
    expected_version: int,
    decision: dict[str, Any],
    status: str,
    decided_by: uuid.UUID | None,
) -> Decided | None:
    """Version-checked transition out of `pending`. None = someone else decided first, the
    version is stale, or the approval no longer exists."""
    row = (
        await conn.execute(
            _DECIDE,
            {
                "id": approval_id,
                "v": expected_version,
                "status": status,
                "d": json.dumps(decision),
                "by": decided_by,
            },
        )
    ).first()
    if row is None:
        return None
    m = row._mapping
    return Decided(
        id=m["id"],
        task_id=m["task_id"],
        kind=m["level"],
        key=m["key"],
        source=m["source"],
        blocking=m["blocking"],
        payload=dict(m["payload"]),
        version=m["version"],
    )


async def apply(conn: AsyncConnection, d: Decided, decision: dict[str, Any], status: str) -> None:
    """Act on a decision that won `decide`, in the same transaction."""
    if d.kind == "side_effect_review":
        # Records only: never re-executes anything (Phase 4 change 1).
        new = "done" if decision["action"] == "confirmed_done" else "failed"
        await conn.execute(
            text(
                "UPDATE side_effects SET status = CAST(:s AS side_effect_status), "
                "error = :e, updated_at = now(), completed_at = coalesce(completed_at, now()) "
                "WHERE idempotency_key = :k AND task_id = :t"
            ),
            {
                "s": new,
                "e": f"resolved by reviewer: {decision['action']}"
                + (f" ({decision['note']})" if decision.get("note") else ""),
                "k": d.payload["idempotency_key"],
                "t": d.task_id,
            },
        )
    elif d.source == "worker":
        await enqueue_on(
            conn,
            "resume",
            thread_id=d.task_id,
            payload={"worker_resume": {"kind": d.kind, "key": d.key, "decision": decision}},
        )
    else:
        await enqueue_on(conn, "resume", thread_id=d.task_id, payload={"resume": decision})
    await emit_on(
        conn,
        d.task_id,
        "approval.decided",
        payload={
            "approval_id": str(d.id),
            "kind": d.kind,
            "key": d.key,
            "status": status,
            "action": decision.get("action"),
        },
        dedupe_key=f"decided:{d.key}"[:200],
    )


async def sweep_expired(engine: AsyncEngine) -> list[uuid.UUID]:
    """Expire overdue blocking approvals through the SAME version-checked `decide` as the API,
    so an expiry racing a human decision can't both win (Phase 4 change 3)."""
    async with engine.connect() as conn:
        due = (
            await conn.execute(
                text(
                    "SELECT id, version FROM approvals WHERE status = 'pending' AND blocking "
                    "AND expires_at < now() ORDER BY expires_at LIMIT 100"
                )
            )
        ).all()
    expired: list[uuid.UUID] = []
    for approval_id, version in due:
        async with engine.begin() as conn:
            d = await decide(
                conn,
                approval_id,
                expected_version=version,
                decision=EXPIRED_DECISION,
                status="expired",
                decided_by=None,
            )
            if d is not None:
                await apply(conn, d, EXPIRED_DECISION, "expired")
                expired.append(d.id)
    return expired
