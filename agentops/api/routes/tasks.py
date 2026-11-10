"""Task endpoints: create, list, detail, trace, cost, and the event stream (SSE).

Ownership: every read goes through `_get_owned` / `owned_by`, so another user's task is a 404.
SSE auth (ADR-041): `POST /tasks/{id}/stream-ticket` issues a single-use ticket; the stream
accepts `?ticket=` (EventSource) or a bearer header (other clients). Never a token in a URL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sse_starlette.sse import EventSourceResponse

from agentops.api import tickets, views
from agentops.api.auth import AuthUser, CurrentUser, get_current_user, owned_by
from agentops.api.sse import event_stream
from agentops.core.budget import open_budget_on
from agentops.db.models import Task, User
from agentops.models.scenarios import SCENARIO_DESCRIPTIONS, SCENARIO_NAMES
from agentops.observability.events import emit_on
from agentops.queue.jobs import enqueue_on
from agentops.settings import Budget, LLMMode, Settings

router = APIRouter(prefix="/tasks", tags=["tasks"])


class BudgetIn(BaseModel):
    max_usd: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_llm_calls: int | None = Field(default=None, gt=0)


class CreateTask(BaseModel):
    request: str = Field(min_length=1, max_length=20_000)
    graph: Literal["main", "demo"] = "main"
    # Fake mode only: which scripted scenario drives the models (models/scenarios.py).
    scenario: str | None = Field(default=None, max_length=64)
    budget: BudgetIn | None = None
    # e.g. ["e2e"]: test and eval runs tag their tasks so the history hides them by default
    tags: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,23}$")]] = Field(
        default_factory=list, max_length=5
    )


# Tags hidden from the task list unless asked for (test / eval traffic, not real work).
HIDDEN_TAGS = ("e2e", "eval")


class TaskOut(BaseModel):
    id: uuid.UUID
    request: str
    graph: str
    status: str
    kind: str
    parent_task_id: uuid.UUID | None
    root_task_id: uuid.UUID | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


def _out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        request=t.request,
        graph=t.graph,
        status=t.status.value,
        kind=t.kind,
        parent_task_id=t.parent_task_id,
        root_task_id=t.root_task_id,
        result=t.result,
        error=t.error,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


async def _get_owned(request: Request, task_id: uuid.UUID, user: CurrentUser) -> Task:
    async with request.app.state.sessionmaker() as s:
        task: Task | None = (
            await s.execute(owned_by(select(Task).where(Task.id == task_id), Task.owner_id, user))
        ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return task


def task_budget(settings: Settings, body: BudgetIn | None) -> Budget:
    """Defaults from settings; user overrides are bounded (422 above the configured caps)."""
    limits = settings.budget
    if body is None:
        return limits
    caps = {
        "max_usd": settings.task_budget_max_usd,
        "max_tokens": settings.task_budget_max_tokens,
        "max_llm_calls": settings.task_budget_max_llm_calls,
    }
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        if v > caps[k]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"budget.{k} must be <= {caps[k]}"
            )
    return limits.model_copy(update=changes)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(body: CreateTask, request: Request, user: AuthUser) -> TaskOut:
    settings = request.app.state.settings
    config: dict[str, Any] = {}
    if body.scenario is not None:
        if settings.llm_mode is not LLMMode.FAKE:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "scenario is only allowed in fake mode"
            )
        if body.scenario not in SCENARIO_NAMES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"unknown scenario; choose one of {sorted(SCENARIO_NAMES)}",
            )
        config["scenario"] = body.scenario
    limits = task_budget(settings, body.budget)
    # The original limits, kept even if a reviewer later raises them: a replay fork defaults
    # to these (ADR-040), not to whatever is left.
    config["budget"] = limits.model_dump(include={"max_usd", "max_tokens", "max_llm_calls"})
    if body.tags:
        config["tags"] = sorted(set(body.tags))
    async with request.app.state.sessionmaker() as s, s.begin():
        task = Task(owner_id=user.id, request=body.request, graph=body.graph, config=config)
        s.add(task)
        await s.flush()
        conn = await s.connection()
        await open_budget_on(conn, task.id, limits)
        await emit_on(conn, task.id, "task.created", payload={"graph": body.graph})
        await enqueue_on(conn, "run", thread_id=task.id, max_attempts=settings.job_max_attempts)
    return _out(await _get_owned(request, task.id, user))


@router.get("/scenarios")
async def list_scenarios(request: Request, user: AuthUser) -> dict[str, Any]:
    """Fake-mode scenarios the UI may offer (empty list in real mode)."""
    fake = request.app.state.settings.llm_mode is LLMMode.FAKE
    names = sorted(SCENARIO_NAMES, key=lambda n: (n != "ui_demo", n)) if fake else []
    return {
        "fake_mode": fake,
        "default": "ui_demo" if fake else None,
        "scenarios": [{"name": n, "description": SCENARIO_DESCRIPTIONS.get(n, "")} for n in names],
    }


class TaskListItem(BaseModel):
    id: uuid.UUID
    request: str
    status: str
    kind: str
    root_task_id: uuid.UUID | None
    scenario: str | None
    cost_usd: float
    llm_calls: int
    duration_s: float
    pending_approvals: int
    branches: int
    created_at: datetime
    updated_at: datetime


class TaskList(BaseModel):
    items: list[TaskListItem]
    next_cursor: str | None


_TERMINAL_SQL = (
    "('completed','completed_partial','failed','rejected','budget_exceeded','cancelled')"
)


@router.get("")
async def list_tasks(
    request: Request,
    user: AuthUser,
    *,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    kind: Literal["task", "replay", "all"] = "task",
    root_task_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    include_hidden: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TaskList:
    """Newest first, keyset-paginated on (created_at, id). Replays are hidden unless asked for
    (kind=replay|all, or root_task_id=<root> to list one task's branches)."""
    where = ["t.owner_id = :uid"]
    p: dict[str, Any] = {"uid": user.id, "lim": limit + 1}
    if root_task_id is not None:
        where.append("t.root_task_id = :root")
        p["root"] = root_task_id
    elif kind != "all":
        where.append("t.kind = :kind")
        p["kind"] = kind
    if not include_hidden:
        where.append("NOT (COALESCE(t.config->'tags', '[]'::jsonb) ?| :hidden)")
        p["hidden"] = list(HIDDEN_TAGS)
    if status_:
        where.append("t.status::text = ANY(:statuses)")
        p["statuses"] = status_
    if q:
        where.append("t.request ILIKE :q")
        p["q"] = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    if created_after:
        where.append("t.created_at >= :after")
        p["after"] = created_after
    if created_before:
        where.append("t.created_at < :before")
        p["before"] = created_before
    if cursor:
        try:
            ts, cid = cursor.split("|", 1)
            p["c_ts"], p["c_id"] = datetime.fromisoformat(ts), uuid.UUID(cid)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
        where.append("(t.created_at, t.id) < (:c_ts, :c_id)")
    # fixed SQL fragments only; every value is a bound parameter
    sql = text(
        "SELECT t.id, t.request, t.status::text AS status, t.kind, t.root_task_id, "  # noqa: S608
        "t.config->>'scenario' AS scenario, t.created_at, t.updated_at, "
        "COALESCE(b.used_usd, 0) AS cost_usd, COALESCE(b.used_calls, 0) AS llm_calls, "
        "EXTRACT(EPOCH FROM (CASE WHEN t.status::text IN "
        f"{_TERMINAL_SQL} THEN t.updated_at ELSE now() END) - t.created_at) AS duration_s, "
        "(SELECT count(*) FROM approvals a WHERE a.task_id = t.id AND a.status = 'pending') "
        "AS pending_approvals, "
        "(SELECT count(*) FROM tasks r WHERE r.root_task_id = t.id) AS branches "
        "FROM tasks t LEFT JOIN task_budgets b ON b.task_id = t.id "
        f"WHERE {' AND '.join(where)} ORDER BY t.created_at DESC, t.id DESC LIMIT :lim"
    )
    async with request.app.state.engine.connect() as c:
        rows = [dict(r._mapping) for r in await c.execute(sql, p)]
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        TaskListItem(
            **{**r, "cost_usd": float(r["cost_usd"]), "duration_s": float(r["duration_s"])}
        )
        for r in rows
    ]
    nxt = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if more and rows else None
    return TaskList(items=items, next_cursor=nxt)


@router.get("/{task_id}")
async def get_task(task_id: uuid.UUID, request: Request, user: AuthUser) -> TaskOut:
    return _out(await _get_owned(request, task_id, user))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, request: Request, user: AuthUser) -> None:
    """Dev/test only: remove a finished task and everything derived from it (replay branches,
    events, calls, approvals, side effects, checkpoints, memories). Refused in production,
    where those records are the audit trail."""
    settings = request.app.state.settings
    if settings.env == "prod":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "deleting tasks is disabled in production (audit records)"
        )
    task = await _get_owned(request, task_id, user)
    if task.status.value not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"task is {task.status.value}; not deleted")
    await purge_tasks(request.app.state.engine, [task_id])


TERMINAL_STATUSES = frozenset(
    {"completed", "completed_partial", "failed", "rejected", "budget_exceeded", "cancelled"}
)


async def purge_tasks(engine: Any, ids: list[uuid.UUID]) -> int:
    """Delete tasks with their replay trees. FKs cascade every table except memories (SET NULL)
    and LangGraph's checkpoint tables (no FK), which are cleared explicitly."""
    async with engine.begin() as c:
        tree = [
            r[0]
            for r in await c.execute(
                text("SELECT id FROM tasks WHERE id = ANY(:ids) OR root_task_id = ANY(:ids)"),
                {"ids": ids},
            )
        ]
        threads = [str(t) for t in tree]
        await c.execute(text("DELETE FROM memories WHERE source_task_id = ANY(:t)"), {"t": tree})
        for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await c.execute(
                text(f"DELETE FROM {tbl} WHERE thread_id = ANY(:t)"),  # noqa: S608
                {"t": threads},
            )
        res = await c.execute(text("DELETE FROM tasks WHERE id = ANY(:t)"), {"t": tree})
    return int(res.rowcount or 0)


@router.get("/{task_id}/detail")
async def task_detail(task_id: uuid.UUID, request: Request, user: AuthUser) -> dict[str, Any]:
    task = await _get_owned(request, task_id, user)
    graph = request.app.state.graphs.get(task.graph) if task.graph == "main" else None
    return await views.task_detail(
        request.app.state.engine, graph, task, request.app.state.settings
    )


@router.get("/{task_id}/trace")
async def task_trace(
    task_id: uuid.UUID,
    request: Request,
    user: AuthUser,
    *,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    kind: Annotated[list[Literal["llm", "tool", "event"]] | None, Query()] = None,
    agent: Annotated[str | None, Query(max_length=32)] = None,
    errors_only: bool = False,
) -> dict[str, Any]:
    await _get_owned(request, task_id, user)
    return await views.trace_page(
        request.app.state.engine,
        task_id,
        after=cursor,
        limit=limit,
        kinds=set(kind) if kind else None,
        agent=agent,
        errors_only=errors_only,
    )


@router.get("/{task_id}/cost")
async def task_cost(task_id: uuid.UUID, request: Request, user: AuthUser) -> dict[str, Any]:
    task = await _get_owned(request, task_id, user)
    return await views.cost_summary(request.app.state.engine, task)


class TicketOut(BaseModel):
    ticket: str
    expires_at: datetime


@router.post("/{task_id}/stream-ticket", status_code=status.HTTP_201_CREATED)
async def stream_ticket(task_id: uuid.UUID, request: Request, user: AuthUser) -> TicketOut:
    await _get_owned(request, task_id, user)
    ticket, expires = await tickets.issue(request.app.state.engine, user.id, task_id)
    return TicketOut(ticket=ticket, expires_at=expires)


_optional_bearer = HTTPBearer(auto_error=False)


async def stream_user(
    task_id: uuid.UUID,
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
    ticket: Annotated[str | None, Query(max_length=128)] = None,
) -> CurrentUser:
    """Ticket (single use, bound to this task) or bearer header. A failed ticket is a 401 and
    never falls back to anything else."""
    if ticket is not None:
        uid = await tickets.redeem(request.app.state.engine, ticket, task_id)
        if uid is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or used stream ticket")
        async with request.app.state.sessionmaker() as s:
            u = await s.get(User, uid)
        if u is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or used stream ticket")
        return CurrentUser(id=u.id, name=u.name, role=u.role)
    return await get_current_user(request, creds)


@router.get("/{task_id}/events")
async def task_events(
    task_id: uuid.UUID,
    request: Request,
    user: Annotated[CurrentUser, Depends(stream_user)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: int | None = None,
) -> EventSourceResponse:
    await _get_owned(request, task_id, user)
    try:
        start = int(last_event_id) if last_event_id else (after or 0)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Last-Event-ID") from None
    return EventSourceResponse(
        event_stream(request.app.state.engine, request.app.state.broadcaster, task_id, start),
        ping=15,
    )
