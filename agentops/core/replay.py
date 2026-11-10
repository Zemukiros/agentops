"""Replay: fork a task at a checkpoint into a new branch (ADR-040).

A branch is a task (kind='replay') with its own thread, so checkpoint ids, event dedupe keys,
ledger, approvals and SSE are all branch-scoped. Forking copies the chosen checkpoint's state
into the new thread with `aupdate_state(as_node=<the node that ran before it>)`, so routing is
recomputed exactly as in the original (probed on LangGraph 1.2.20 at every fork point).
Upstream nodes never run again in the branch: their recorded model/tool results are reused.

Side effects never repeat in a branch:
- action_gate reuses the decision the original branch made for the same action (it never asks
  about actions the original didn't decide; those are not executed)
- execute_actions never performs anything: it reports what the original branch did
- no memories are written (consolidate and the worker's episode both skip)

Workspace: `schedule` snapshots the task workspace at every wave boundary (the only place
files change is inside waves). A branch gets its own copy of the snapshot matching its fork
point, so it never writes into the original's workspace and never misses a file.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentops.core import scheduler
from agentops.core.state import Plan, ReplayContext, SubtaskResult
from agentops.settings import Settings

__all__ = ["ForkEdits", "ReplayContext", "ReplayError"]

# The single next node a branch may start at, and the node whose output it continues from is
# read from the parent checkpoint. Mid-wave points (pending Sends) are never offered.
FORKABLE_NEXT = ("plan", "plan_gate", "schedule", "action_gate", "synthesize", "final_review")
FORKABLE_TASK_STATUSES = frozenset(
    {
        "completed",
        "completed_partial",
        "failed",
        "rejected",
        "budget_exceeded",
        "cancelled",
        "awaiting_approval",
    }
)


class ReplayError(ValueError):
    pass


class ForkEdits(BaseModel):
    """The state fields a fork may change. Each is allowed only at certain fork points."""

    model_config = ConfigDict(extra="forbid")
    request: str | None = Field(default=None, min_length=1, max_length=20_000)
    plan: Plan | None = None
    outputs: dict[str, str] = Field(default_factory=dict)  # accepted subtask id -> new output
    final: str | None = Field(default=None, min_length=1, max_length=50_000)

    def is_empty(self) -> bool:
        return not (self.request or self.plan or self.outputs or self.final)


def editable_fields(next_node: str, wave: int) -> list[str]:
    if next_node == "plan":
        return ["request"]
    if next_node == "plan_gate" or (next_node == "schedule" and wave == 0):
        return ["plan"]
    if next_node in {"schedule", "action_gate", "synthesize"}:
        return ["outputs"]
    if next_node == "final_review":
        return ["final"]
    return []


def apply_edits(
    values: dict[str, Any], next_node: str, edits: ForkEdits, settings: Settings
) -> dict[str, Any]:
    """A copy of `values` with validated edits applied. Raises ReplayError."""
    wave = int(values.get("wave") or 0)
    allowed = set(editable_fields(next_node, wave))
    used = {k for k in ("request", "plan", "outputs", "final") if getattr(edits, k)}
    if used - allowed:
        raise ReplayError(
            f"cannot edit {sorted(used - allowed)} at a fork before {next_node!r}; "
            f"editable here: {sorted(allowed) or 'nothing'}"
        )
    out = dict(values)
    if edits.request:
        out["request"] = edits.request
    if edits.plan is not None:
        errors = scheduler.validate_plan(edits.plan)
        if errors:
            raise ReplayError("invalid plan: " + "; ".join(errors))
        out["plan"] = edits.plan
        out["plan_confidence"] = scheduler.plan_confidence(
            edits.plan,
            memory_similarity=float(values.get("memory_similarity") or 0.0),
            threshold=settings.plan_confidence_threshold,
        )
    if edits.outputs:
        results: dict[str, SubtaskResult] = dict(values.get("results") or {})
        for sid, text_ in edits.outputs.items():
            r = results.get(sid)
            if r is None or r.status != "accepted":
                raise ReplayError(f"subtask {sid!r} has no accepted output to edit")
            if not text_.strip() or len(text_) > 50_000:
                raise ReplayError(f"output for {sid!r} must be 1..50000 characters")
            results[sid] = r.model_copy(update={"output": text_})
        out["results"] = results
    if edits.final:
        out["final"] = edits.final
    return out


# ---- checkpoints ---------------------------------------------------------------------------
async def checkpoints(
    graph: Any, task_id: uuid.UUID, *, origin_as_node: str | None = None
) -> list[dict[str, Any]]:
    """The thread's checkpoints, newest first, with forkability and the as_node to fork with.
    A branch's first checkpoint has no parent in its thread; `origin_as_node` (recorded when the
    branch was forked) stands in for it, so branches can be forked again."""
    cfg = {"configurable": {"thread_id": str(task_id)}}
    hist = [h async for h in graph.aget_state_history(cfg)]
    by_id = {h.config["configurable"]["checkpoint_id"]: h for h in hist}
    out = []
    for h in hist:
        cid = h.config["configurable"]["checkpoint_id"]
        parent = (
            by_id.get(h.parent_config["configurable"]["checkpoint_id"]) if h.parent_config else None
        )
        as_node = parent.next[0] if parent is not None and parent.next else None
        if parent is None and (h.metadata or {}).get("source") == "update":
            as_node = origin_as_node
        nxt = list(h.next)
        wave = int(h.values.get("wave") or 0)
        reason = None
        if not nxt:
            reason = "end of the run"
        elif len(set(nxt)) != 1 or len(nxt) != 1:
            reason = "inside a parallel wave"
        elif nxt[0] not in FORKABLE_NEXT:
            reason = f"not a fork point ({nxt[0]})"
        elif as_node is None:
            reason = "no preceding step"
        results = h.values.get("results") or {}
        out.append(
            {
                "checkpoint_id": cid,
                "step": (h.metadata or {}).get("step"),
                "created_at": h.created_at,
                "next": nxt,
                "wave": wave,
                "forkable": reason is None,
                "reason": reason,
                "as_node": as_node,
                "editable": editable_fields(nxt[0], wave) if reason is None else [],
                "subtasks": {sid: r.status for sid, r in results.items()},
                "has_final": bool(h.values.get("final")),
            }
        )
    return out


async def checkpoint_values(graph: Any, task_id: uuid.UUID, checkpoint_id: str) -> dict[str, Any]:
    cfg = {"configurable": {"thread_id": str(task_id), "checkpoint_id": checkpoint_id}}
    snap = await graph.aget_state(cfg)
    if not snap.values:
        raise ReplayError("checkpoint not found")
    return dict(snap.values)


# ---- lineage: what the original branch decided / did ---------------------------------------
async def lineage(conn: AsyncConnection | AsyncEngine, task_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """Ancestors of a branch, nearest first. Empty for an ordinary task."""
    sql = text(
        "WITH RECURSIVE up(id, parent, depth) AS ("
        "  SELECT id, parent_task_id, 0 FROM tasks WHERE id = :t"
        "  UNION ALL SELECT t.id, t.parent_task_id, up.depth + 1 FROM tasks t "
        "  JOIN up ON t.id = up.parent WHERE up.depth < 50"
        ") SELECT id FROM up WHERE depth > 0 ORDER BY depth"
    )
    if isinstance(conn, AsyncEngine):
        async with conn.connect() as c:
            rows = (await c.execute(sql, {"t": task_id})).scalars().all()
    else:
        rows = (await conn.execute(sql, {"t": task_id})).scalars().all()
    return tuple(rows)


async def original_decision(
    engine: AsyncEngine, ancestors: tuple[uuid.UUID, ...], key: str
) -> dict[str, Any] | None:
    """The nearest ancestor's resolved decision for an approval key (None if never decided)."""
    if not ancestors:
        return None
    async with engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT task_id, decision FROM approvals WHERE key = :k "
                    "AND task_id = ANY(:ids) AND status <> 'pending' AND decision IS NOT NULL"
                ),
                {"k": key, "ids": list(ancestors)},
            )
        ).all()
    by_task = {r.task_id: r.decision for r in rows}
    for a in ancestors:
        if a in by_task:
            return dict(by_task[a])
    return None


async def original_side_effect(
    engine: AsyncEngine, ancestors: tuple[uuid.UUID, ...], keys: list[str]
) -> tuple[str, str, str | None] | None:
    """(idempotency key, status, error) of the nearest ancestor's side effect among `keys`."""
    if not keys:
        return None
    async with engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT idempotency_key, task_id, status::text AS status, error "
                    "FROM side_effects WHERE idempotency_key = ANY(:k)"
                ),
                {"k": keys},
            )
        ).all()
    by_task = {r.task_id: r for r in rows}
    for a in ancestors:
        if a in by_task:
            r = by_task[a]
            return r.idempotency_key, r.status, r.error
    return None


# ---- workspace snapshots -------------------------------------------------------------------
def snapshots_root(settings: Settings) -> Path:
    # A sibling of the task workspaces: never inside one, so never visible to tools/sandbox.
    return settings.workspace_root.resolve() / ".snapshots"


def _open_permissions(root: Path) -> None:
    # The sandbox runs as uid 10001 and must be able to read and write the copy.
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o777)  # noqa: S103
        for f in filenames:
            p = Path(dirpath) / f
            if not p.is_symlink():
                os.chmod(p, 0o666)  # noqa: S103
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]


def snapshot_workspace(settings: Settings, task_id: uuid.UUID, wave: int) -> Path:
    """Copy the task workspace to .snapshots/<task>/w<wave> (atomic replace; idempotent:
    a re-run of the same schedule pass sees the same files)."""
    src = settings.workspace_root.resolve() / str(task_id)
    dest = snapshots_root(settings) / str(task_id) / f"w{wave}"
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}")
    # Writable by any worker (container root, host user, sandbox uid) sharing the volume, like
    # the task workspaces themselves (ADR-046).
    for d in (snapshots_root(settings), tmp.parent):
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):
            os.chmod(d, 0o777)  # noqa: S103
    if src.is_dir():
        shutil.copytree(src, tmp, symlinks=True)
    else:
        tmp.mkdir()
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return dest


def restore_workspace(
    settings: Settings,
    fork_id: uuid.UUID,
    source_id: uuid.UUID,
    wave: int,
    source_live_wave: int | None,
) -> str:
    """Give a branch its own copy of the source workspace as it was at the fork point.
    Idempotent (a marker outside the workspace records completion). Returns how."""
    marker = snapshots_root(settings) / str(fork_id) / ".restored"
    if marker.exists():
        return "already restored"
    snap = snapshots_root(settings) / str(source_id) / f"w{wave}"
    live = settings.workspace_root.resolve() / str(source_id)
    if snap.is_dir():
        src, how = snap, f"snapshot w{wave}"
    elif wave == 0:
        src, how = None, "empty (fork before the first wave)"
    elif source_live_wave == wave and live.is_dir():
        # No wave ran after the fork point, and the source is paused/terminal: unchanged.
        src, how = live, f"source workspace (no wave after w{wave})"
    else:
        raise ReplayError(f"workspace snapshot w{wave} of {source_id} is missing")
    dest = settings.workspace_root.resolve() / str(fork_id)
    if dest.exists():
        shutil.rmtree(dest)  # a partial copy from a crashed attempt
    if src is not None:
        shutil.copytree(src, dest, symlinks=True)
    else:
        dest.mkdir(parents=True)
    _open_permissions(dest)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return how
