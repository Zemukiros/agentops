"""Integration fixtures: a fresh `agentops_test` database per session (migrated with the real
Alembic migration + checkpointer setup), truncated between tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentops.api.auth import hash_token
from agentops.api.main import create_app
from agentops.core.budget import open_budget_on
from agentops.db.session import get_sessionmaker, make_engine
from agentops.settings import Budget, get_settings

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "events, jobs, llm_calls, tool_calls, approvals, side_effects, memories, "
    "budget_reservations, task_budgets, tasks, users, "
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


async def create_task(
    engine: AsyncEngine,
    owner: SeededUser,
    request: str = "probe",
    *,
    graph: str = "demo",
    config: dict | None = None,
    budget: Budget | None = None,
) -> uuid.UUID:
    """Insert a task and its budget ledger row (as POST /tasks does)."""
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, request, graph, config) "
                "VALUES (:i, :o, :r, :g, CAST(:c AS jsonb))"
            ),
            {"i": tid, "o": owner.id, "r": request, "g": graph, "c": json.dumps(config or {})},
        )
        await open_budget_on(conn, tid, budget or get_settings().budget)
    return tid


@pytest.fixture
async def app(test_db: str) -> AsyncIterator[FastAPI]:
    application = create_app(get_settings())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=20) as c:
        yield c


def parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
        if data:
            events.append(json.loads("".join(data)))
    return events


async def graph_state_values(tid: uuid.UUID) -> dict:
    """The main graph's current state for a task (e.g. proposals while paused at action_gate)."""
    from agentops.core.graph import build_main_graph  # noqa: PLC0415
    from agentops.db.checkpointer import open_checkpointer  # noqa: PLC0415

    async with open_checkpointer(get_settings().libpq_dsn, max_size=1) as saver:
        snap = await build_main_graph(saver).aget_state({"configurable": {"thread_id": str(tid)}})
    return dict(snap.values)


async def pending_approvals(engine: AsyncEngine, tid: uuid.UUID) -> list[tuple[str, str]]:
    async with engine.connect() as c:
        rows = await c.execute(
            text(
                "SELECT level, key FROM approvals WHERE task_id=:t AND status='pending' "
                "ORDER BY key"
            ),
            {"t": tid},
        )
        return [(r[0], r[1]) for r in rows]


async def work(
    engine: AsyncEngine,
    saver: object,
    *,
    settings: object | None = None,
    chat_factory: object | None = None,
    tools: object | None = None,
) -> None:
    """Let an in-process worker handle the next queued job."""
    from agentops.queue import jobs  # noqa: PLC0415
    from agentops.queue.worker import Worker  # noqa: PLC0415

    s = settings or get_settings()
    job = await jobs.claim(engine, "w-conftest", s.job_lease_seconds)  # type: ignore[attr-defined]
    assert job is not None, "no job queued"
    w = Worker(s, engine, saver, "w-conftest", chat_factory=chat_factory, tools=tools)  # type: ignore[arg-type]
    await w.process(job)


async def decide(
    engine: AsyncEngine, tid: uuid.UUID, kind: str, raw: dict, user_id: uuid.UUID | None = None
) -> dict:
    """Decide the task's pending approval of `kind` exactly as the API does."""
    from agentops import approvals  # noqa: PLC0415

    async with engine.connect() as c:
        row = (
            await c.execute(
                text(
                    "SELECT id, version, payload FROM approvals WHERE task_id=:t AND level=:k "
                    "AND status='pending'"
                ),
                {"t": tid, "k": kind},
            )
        ).one()
    decision = approvals.validate_decision(kind, raw, dict(row.payload), get_settings())
    status = approvals.STATUS_FOR_ACTION[decision["action"]]
    async with engine.begin() as c:
        won = await approvals.decide(
            c,
            row.id,
            expected_version=row.version,
            decision=decision,
            status=status,
            decided_by=user_id,
        )
        assert won is not None
        await approvals.apply(c, won, decision, status)
    return decision
