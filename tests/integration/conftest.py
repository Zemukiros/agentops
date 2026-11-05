"""Integration fixtures: a fresh `agentops_test` database per session (migrated with the real
Alembic migration + checkpointer setup), truncated between tests."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentops.api.auth import hash_token
from agentops.db.session import get_sessionmaker, make_engine
from agentops.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "events, jobs, llm_calls, tool_calls, approvals, side_effects, memories, tasks, users, "
    "checkpoints, checkpoint_blobs, checkpoint_writes"
)


def _admin_dsn(test_dsn: str) -> tuple[str, str]:
    base, dbname = test_dsn.rsplit("/", 1)
    return base + "/postgres", dbname


@pytest.fixture(scope="session")
def test_db() -> Iterator[str]:
    dsn = get_settings().libpq_dsn
    admin, dbname = _admin_dsn(dsn)
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{dbname}"')
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres not reachable ({exc}). Run `make up` first.", pytrace=False)
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    for cmd in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "agentops.db.checkpointer"],
    ):
        subprocess.run(cmd, cwd=ROOT, env=env, check=True, capture_output=True)
    yield dsn


@pytest.fixture(scope="session")
async def engine(test_db: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(os.environ["TEST_DATABASE_URL"], pool_size=10)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return get_sessionmaker(engine)


@pytest.fixture(autouse=True)
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


@dataclass(frozen=True)
class SeededUser:
    id: uuid.UUID
    name: str
    token: str
    role: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _make_user(engine: AsyncEngine, name: str, role: str) -> SeededUser:
    token = f"tok-{name}-{uuid.uuid4().hex}"
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, name, token_hash, role) "
                "VALUES (:i, :n, :h, CAST(:r AS user_role))"
            ),
            {"i": uid, "n": name, "h": hash_token(token), "r": role},
        )
    return SeededUser(uid, name, token, role)


@pytest.fixture
async def alice(engine: AsyncEngine, clean_db: None) -> SeededUser:
    return await _make_user(engine, "alice", "user")


@pytest.fixture
async def bob(engine: AsyncEngine, clean_db: None) -> SeededUser:
    return await _make_user(engine, "bob", "user")


@pytest.fixture
async def reviewer(engine: AsyncEngine, clean_db: None) -> SeededUser:
    return await _make_user(engine, "rita", "reviewer")


async def create_task(engine: AsyncEngine, owner: SeededUser, request: str = "probe") -> uuid.UUID:
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks (id, owner_id, request, graph) VALUES (:i, :o, :r, 'demo')"),
            {"i": tid, "o": owner.id, "r": request},
        )
    return tid
