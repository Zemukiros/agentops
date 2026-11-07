"""Memory API (ADR-038). Every route is scoped to the caller's own memories; another user's
memory is indistinguishable from a missing one (404)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from agentops.api.auth import AuthUser
from agentops.memory import store
from agentops.memory.service import MemoryService

router = APIRouter(prefix="/memories", tags=["memories"])


def _service(request: Request) -> MemoryService:
    svc: MemoryService | None = getattr(request.app.state, "memory", None)
    if svc is None:
        svc = MemoryService(request.app.state.settings, request.app.state.engine)
        request.app.state.memory = svc
    return svc


@router.get("")
async def list_memories(
    request: Request,
    user: AuthUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    rows = await store.list_for(request.app.state.engine, user.id, limit=limit, offset=offset)
    return [r.as_dict() for r in rows]


@router.get("/search")
async def search_memories(
    request: Request,
    user: AuthUser,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    k: Annotated[int, Query(ge=1, le=20)] = 5,
    min_similarity: Annotated[float | None, Query(ge=-1, le=1)] = None,
) -> list[dict[str, Any]]:
    svc = _service(request)
    [vec] = await svc.embedder.embed([q])
    rows = await store.recall(
        request.app.state.engine,
        svc.settings,
        user_id=user.id,
        query_embedding=vec,
        min_similarity=min_similarity,
        top_k=k,
    )
    return [r.as_dict() for r in rows]


@router.get("/{memory_id}")
async def get_memory(memory_id: uuid.UUID, request: Request, user: AuthUser) -> dict[str, Any]:
    row = await store.get(request.app.state.engine, user.id, memory_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    return row.as_dict()


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: uuid.UUID, request: Request, user: AuthUser) -> None:
    if not await store.delete_one(request.app.state.engine, user.id, memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")


@router.delete("")
async def delete_all_memories(request: Request, user: AuthUser) -> dict[str, int]:
    """Every memory of the caller, in one transaction."""
    return {"deleted": await store.delete_all(request.app.state.engine, user.id)}
