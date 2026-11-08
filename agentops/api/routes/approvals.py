"""Approvals inbox (ADR-035).

GET  /approvals                  reviewers: all tasks; users: their own tasks only
GET  /approvals/{id}             same visibility (invisible -> 404)
POST /approvals/{id}/decision    reviewer role; {"version": n, "decision": {...}}
                                 422 invalid decision, 409 stale version / already decided
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from agentops import approvals
from agentops.api.auth import AuthUser, CurrentUser, require_role
from agentops.db.models import UserRole

router = APIRouter(prefix="/approvals", tags=["approvals"])
Reviewer = Annotated[CurrentUser, require_role(UserRole.REVIEWER)]

_COLS = (
    "a.id, a.task_id, a.level AS kind, a.key, a.status::text AS status, a.version, a.payload, "
    "a.decision, a.source, a.blocking, a.expires_at, a.created_at, a.decided_at, a.decided_by, "
    "t.kind AS task_kind, t.request AS task_request"
)


class ApprovalOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    key: str
    status: str
    version: int
    payload: dict[str, Any]
    decision: dict[str, Any] | None
    source: str
    blocking: bool
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    decided_by: uuid.UUID | None
    task_kind: str  # 'replay' items come from a replay branch (ADR-040)
    task_request: str


class DecisionIn(BaseModel):
    version: int = Field(ge=1)
    decision: dict[str, Any]


def _visible(user: CurrentUser) -> tuple[str, dict[str, Any]]:
    if user.is_reviewer:
        return "TRUE", {}
    return "t.owner_id = :uid", {"uid": user.id}


async def _load(request: Request, approval_id: uuid.UUID, user: CurrentUser) -> ApprovalOut:
    where, params = _visible(user)
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_COLS} FROM approvals a JOIN tasks t ON t.id = a.task_id "  # noqa: S608
                    f"WHERE a.id = :id AND {where}"
                ),
                {"id": approval_id, **params},
            )
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    return ApprovalOut(**row._mapping)


@router.get("")
async def list_approvals(
    request: Request,
    user: AuthUser,
    state: Literal["pending", "decided", "all"] = "pending",
    task_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ApprovalOut]:
    where, params = _visible(user)
    clauses = [where]
    if state == "pending":
        clauses.append("a.status = 'pending'")
    elif state == "decided":
        clauses.append("a.status <> 'pending'")
    if task_id is not None:
        clauses.append("a.task_id = :task")
        params["task"] = task_id
    async with request.app.state.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLS} FROM approvals a JOIN tasks t ON t.id = a.task_id "  # noqa: S608
                    f"WHERE {' AND '.join(clauses)} ORDER BY a.created_at DESC LIMIT :lim"
                ),
                {**params, "lim": limit},
            )
        ).all()
    return [ApprovalOut(**r._mapping) for r in rows]


@router.get("/{approval_id}")
async def get_approval(approval_id: uuid.UUID, request: Request, user: AuthUser) -> ApprovalOut:
    return await _load(request, approval_id, user)


@router.post("/{approval_id}/decision")
async def decide(
    approval_id: uuid.UUID, body: DecisionIn, request: Request, user: Reviewer
) -> ApprovalOut:
    current = await _load(request, approval_id, user)
    if current.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"approval already {current.status}")
    try:
        decision = approvals.validate_decision(
            current.kind, body.decision, current.payload, request.app.state.settings
        )
    except approvals.DecisionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    new_status = approvals.STATUS_FOR_ACTION[decision["action"]]
    async with request.app.state.engine.begin() as conn:
        won = await approvals.decide(
            conn,
            approval_id,
            expected_version=body.version,
            decision=decision,
            status=new_status,
            decided_by=user.id,
        )
        if won is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "approval changed or was already decided; reload it"
            )
        await approvals.apply(conn, won, decision, new_status)
    return await _load(request, approval_id, user)
