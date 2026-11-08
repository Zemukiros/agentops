"""Replay API (ADR-040): list a task's checkpoints, fork a branch at one (with allowed edits),
and compare two branches. A fork is enqueued immediately and runs like any task."""

from __future__ import annotations

import difflib
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from agentops.api import views
from agentops.api.auth import AuthUser
from agentops.api.routes.tasks import BudgetIn, TaskOut, _get_owned, _out
from agentops.core import replay
from agentops.core.budget import open_budget_on
from agentops.db.models import Task
from agentops.observability.events import emit_on
from agentops.queue.jobs import enqueue_on
from agentops.settings import Budget, Settings

router = APIRouter(prefix="/tasks", tags=["replay"])


def original_budget(settings: Settings, parent: Task) -> Budget:
    """The parent's ORIGINAL per-task limits (not what's left, not a reviewer's raise)."""
    stored = (parent.config or {}).get("budget") or {}
    return settings.budget.model_copy(update={k: v for k, v in stored.items() if v is not None})


def _fork_budget(settings: Settings, parent: Task, body: BudgetIn | None) -> Budget:
    limits = original_budget(settings, parent)
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


def _origin(t: Task) -> str | None:
    return ((t.config or {}).get("fork") or {}).get("as_node")


def _forkable_task(t: Task) -> str | None:
    if t.graph != "main":
        return "only the main workflow can be replayed"
    if t.status.value not in replay.FORKABLE_TASK_STATUSES:
        return f"the task is {t.status.value}; fork it when it is paused or finished"
    return None


@router.get("/{task_id}/checkpoints")
async def list_checkpoints(task_id: uuid.UUID, request: Request, user: AuthUser) -> dict[str, Any]:
    task = await _get_owned(request, task_id, user)
    settings = request.app.state.settings
    cps = (
        await replay.checkpoints(
            request.app.state.graphs["main"], task_id, origin_as_node=_origin(task)
        )
        if task.graph == "main"
        else []
    )
    budget = original_budget(settings, task)
    return {
        "task_id": str(task_id),
        "task_status": task.status.value,
        "fork_blocked_reason": _forkable_task(task),
        "default_budget": budget.model_dump(include={"max_usd", "max_tokens", "max_llm_calls"}),
        "checkpoints": cps,  # StateSnapshot.created_at is already an ISO string
    }


class ForkIn(BaseModel):
    checkpoint_id: str = Field(min_length=1, max_length=64)
    edits: replay.ForkEdits = Field(default_factory=replay.ForkEdits)
    budget: BudgetIn | None = None


@router.post("/{task_id}/branches", status_code=status.HTTP_201_CREATED)
async def fork_task(task_id: uuid.UUID, body: ForkIn, request: Request, user: AuthUser) -> TaskOut:
    parent = await _get_owned(request, task_id, user)
    blocked = _forkable_task(parent)
    if blocked:
        raise HTTPException(status.HTTP_409_CONFLICT, blocked)
    settings = request.app.state.settings
    graph = request.app.state.graphs["main"]
    cp = next(
        (
            c
            for c in await replay.checkpoints(graph, task_id, origin_as_node=_origin(parent))
            if c["checkpoint_id"] == body.checkpoint_id
        ),
        None,
    )
    if cp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "checkpoint not found for this task")
    if not cp["forkable"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"not forkable: {cp['reason']}")
    nxt = cp["next"][0]
    try:
        values = replay.apply_edits(
            await replay.checkpoint_values(graph, task_id, body.checkpoint_id),
            nxt,
            body.edits,
            settings,
        )
    except replay.ReplayError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    limits = _fork_budget(settings, parent, body.budget)

    fork_id = uuid.uuid4()
    values["task_id"] = str(fork_id)
    config: dict[str, Any] = {
        "budget": limits.model_dump(include={"max_usd", "max_tokens", "max_llm_calls"}),
        "fork": {
            "source_task_id": str(parent.id),
            "checkpoint_id": body.checkpoint_id,
            "next": nxt,
            "wave": cp["wave"],
            "as_node": cp["as_node"],
            "edits": body.edits.model_dump(mode="json", exclude_defaults=True),
        },
    }
    if (parent.config or {}).get("scenario"):
        config["scenario"] = parent.config["scenario"]
    async with request.app.state.sessionmaker() as s, s.begin():
        s.add(
            Task(
                id=fork_id,
                owner_id=user.id,
                request=values["request"],
                graph="main",
                config=config,
                kind="replay",
                parent_task_id=parent.id,
                root_task_id=parent.root_task_id or parent.id,
                fork_checkpoint_id=body.checkpoint_id,
            )
        )
        await s.flush()
        conn = await s.connection()
        await open_budget_on(conn, fork_id, limits)
        # The branch's first checkpoint, in its own thread: the copied state, written as if
        # the node that preceded the fork point had produced it (routing recomputed).
        await graph.aupdate_state(
            {"configurable": {"thread_id": str(fork_id)}}, values, as_node=cp["as_node"]
        )
        await emit_on(
            conn,
            fork_id,
            "task.created",
            payload={"graph": "main", "replay": config["fork"]},
        )
        await enqueue_on(conn, "run", thread_id=fork_id, max_attempts=settings.job_max_attempts)
    return _out(await _get_owned(request, fork_id, user))


def _summary(detail: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": detail["id"],
        "kind": detail["kind"],
        "status": detail["status"],
        "request": detail["request"],
        "fork_checkpoint_id": detail["fork_checkpoint_id"],
        "explanation": detail["explanation"],
        "final": detail["final"],
        "final_review": detail["final_review"],
        "subtasks": [
            {
                "id": s["id"],
                "status": s["status"],
                "revisions": s["revisions"],
                "score": s["reviews"][-1]["score"] if s["reviews"] else None,
                "output": s["output"],
            }
            for s in detail["subtasks"]
        ],
        "actions": [
            {
                "tool": a["tool"],
                "decision": a["decision"],
                "status": (a["outcome"] or {}).get("status"),
                "detail": (a["outcome"] or {}).get("detail"),
            }
            for a in detail["actions"]
        ],
        "cost": cost["total"],
        "time": cost["time"],
    }


@router.get("/{task_id}/compare/{other_id}")
async def compare(
    task_id: uuid.UUID, other_id: uuid.UUID, request: Request, user: AuthUser
) -> dict[str, Any]:
    a = await _get_owned(request, task_id, user)
    b = await _get_owned(request, other_id, user)
    if (a.root_task_id or a.id) != (b.root_task_id or b.id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "these tasks are not branches of the same task"
        )
    engine, settings = request.app.state.engine, request.app.state.settings
    graph = request.app.state.graphs["main"]
    sides = []
    for t in (a, b):
        sides.append(
            _summary(
                await views.task_detail(engine, graph, t, settings),
                await views.cost_summary(engine, t),
            )
        )
    left, right = sides
    diff = list(
        difflib.unified_diff(
            (left["final"] or "").splitlines(),
            (right["final"] or "").splitlines(),
            fromfile=f"{a.kind}:{str(a.id)[:8]}",
            tofile=f"{b.kind}:{str(b.id)[:8]}",
            lineterm="",
        )
    )
    l_sub = {s["id"]: s for s in left["subtasks"]}
    r_sub = {s["id"]: s for s in right["subtasks"]}
    subtask_changes = [
        {
            "id": sid,
            "left": l_sub.get(sid, {}).get("status"),
            "right": r_sub.get(sid, {}).get("status"),
            "output_changed": l_sub.get(sid, {}).get("output") != r_sub.get(sid, {}).get("output"),
        }
        for sid in sorted(set(l_sub) | set(r_sub))
    ]
    return {
        "left": left,
        "right": right,
        "final_diff": diff,
        "subtask_changes": subtask_changes,
        "status_changed": left["status"] != right["status"],
    }


@router.get("/{task_id}/lineage")
async def task_lineage(task_id: uuid.UUID, request: Request, user: AuthUser) -> dict[str, Any]:
    """The root and every branch of the tree this task belongs to (for the compare picker)."""
    t = await _get_owned(request, task_id, user)
    root = t.root_task_id or t.id
    async with request.app.state.engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT id, kind, parent_task_id, fork_checkpoint_id, status::text AS status, "
                    "created_at FROM tasks WHERE owner_id = :u AND (id = :r OR root_task_id = :r) "
                    "ORDER BY created_at"
                ),
                {"u": user.id, "r": root},
            )
        ).all()
    return {
        "root_task_id": str(root),
        "tasks": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "parent_task_id": str(r.parent_task_id) if r.parent_task_id else None,
                "fork_checkpoint_id": r.fork_checkpoint_id,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }
