"""Task endpoints. Phase 1 exposes only what the durability probe and SSE need."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from agentops.api.auth import AuthUser, owned_by
from agentops.api.sse import event_stream
from agentops.core.budget import open_budget_on
from agentops.db.models import Task
from agentops.observability.events import emit_on
from agentops.queue.jobs import enqueue_on

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTask(BaseModel):
    request: str = Field(min_length=1, max_length=20_000)
    graph: Literal["demo"] = "demo"  # the real workflow graph is added in Phase 2


class TaskOut(BaseModel):
    id: uuid.UUID
    request: str
    graph: str
    status: str
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
        result=t.result,
        error=t.error,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


async def _get_owned(request: Request, task_id: uuid.UUID, user: AuthUser) -> Task:
    async with request.app.state.sessionmaker() as s:
        task: Task | None = (
            await s.execute(owned_by(select(Task).where(Task.id == task_id), Task.owner_id, user))
        ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return task


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(body: CreateTask, request: Request, user: AuthUser) -> TaskOut:
    settings = request.app.state.settings
    async with request.app.state.sessionmaker() as s, s.begin():
        task = Task(owner_id=user.id, request=body.request, graph=body.graph)
        s.add(task)
        await s.flush()
        conn = await s.connection()
        await open_budget_on(conn, task.id, settings.budget)
        await emit_on(conn, task.id, "task.created", payload={"graph": body.graph})
        await enqueue_on(conn, "run", thread_id=task.id, max_attempts=settings.job_max_attempts)
    return await get_task(task.id, request, user)


@router.get("/{task_id}")
async def get_task(task_id: uuid.UUID, request: Request, user: AuthUser) -> TaskOut:
    return _out(await _get_owned(request, task_id, user))


@router.get("/{task_id}/events")
async def task_events(
    task_id: uuid.UUID,
    request: Request,
    user: AuthUser,
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
