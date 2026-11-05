from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agentops.api.auth import AuthUser

router = APIRouter()

CHECK_TIMEOUT_S = 3.0


async def _db(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _checkpointer(request: Request) -> None:
    saver = request.app.state.checkpointer
    await saver.aget_tuple({"configurable": {"thread_id": "__health__", "checkpoint_ns": ""}})


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """DB and checkpointer reachable -> 200, otherwise 503 with per-check detail."""
    checks: dict[str, Any] = {}
    for name, fn in (("database", _db), ("checkpointer", _checkpointer)):
        try:
            await asyncio.wait_for(fn(request), CHECK_TIMEOUT_S)
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if ok else "degraded", "checks": checks}
    return JSONResponse(body, 200 if ok else 503)


@router.get("/me")
async def me(user: AuthUser) -> dict[str, str]:
    return {"id": str(user.id), "name": user.name, "role": user.role.value}
