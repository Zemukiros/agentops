"""API rate limits (Phase 6 hardening): a token bucket per user and route group, on the same
Postgres table the tool limiter uses. Exceeding it is a 429 with Retry-After."""

from __future__ import annotations

import math
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from agentops.api.auth import CurrentUser, get_current_user
from agentops.tools.ratelimit import try_take


def rate_limit(group: str) -> Any:
    async def dep(
        request: Request, user: Annotated[CurrentUser, Depends(get_current_user)]
    ) -> CurrentUser:
        settings = request.app.state.settings
        per_min = settings.api_rate_limits.get(group)
        if per_min and not await try_take(
            request.app.state.engine, f"api:{group}:{user.id}", per_min
        ):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"rate limit for {group} reached ({per_min}/min)",
                headers={"Retry-After": str(max(1, math.ceil(60 / per_min)))},
            )
        return user

    return Depends(dep)
