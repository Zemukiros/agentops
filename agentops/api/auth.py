"""Bearer-token auth (ADR-012) and ownership helpers.

Tokens are random 256-bit strings, so a keyed fast hash (HMAC-SHA256 with a server pepper) is
sufficient and allows indexed lookup. A slow password hash (argon2) buys nothing for
high-entropy tokens (ADR-018).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, select

from agentops.db.models import User, UserRole
from agentops.settings import get_settings

_bearer = HTTPBearer(auto_error=False)


def hash_token(token: str, pepper: str | None = None) -> str:
    key = (pepper or get_settings().api_token_pepper.get_secret_value()).encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    name: str
    role: UserRole

    @property
    def is_reviewer(self) -> bool:
        return self.role is UserRole.REVIEWER


async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    sm = request.app.state.sessionmaker
    async with sm() as s:
        user = (
            await s.execute(select(User).where(User.token_hash == hash_token(creds.credentials)))
        ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid token", headers={"WWW-Authenticate": "Bearer"}
        )
    return CurrentUser(id=user.id, name=user.name, role=user.role)


def require_role(role: UserRole) -> Any:
    async def dep(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if user.role is not role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role.value}")
        return user

    return Depends(dep)


def owned_by[S: Select[Any]](stmt: S, owner_column: Any, user: CurrentUser) -> S:
    """Restrict a query to rows owned by `user`. Every task/event/memory query goes through
    this (or an equivalent join) so cross-tenant reads return nothing -> 404, not 403."""
    return stmt.where(owner_column == user.id)


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
