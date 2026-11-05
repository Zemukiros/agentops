"""Async SQLAlchemy engine/session factory (psycopg3 driver)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentops.settings import get_settings


def make_engine(url: str | None = None, *, pool_size: int | None = None) -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        url or s.sqlalchemy_url,
        pool_size=pool_size or s.db_pool_size,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return make_engine()


def get_sessionmaker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine or get_engine(), expire_on_commit=False)


async def session_dependency() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
