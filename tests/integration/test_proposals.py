"""ProposedAction (ADR-003, Phase 3 step 7): no specialist path executes a write outside the
task, an external or a destructive tool. Native and MCP tools; static and dynamic checks."""

import ast
import dataclasses
import sqlite3
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.db.checkpointer import open_checkpointer
from agentops.models.client import fake_factory
from agentops.models.fake import ScriptedTurn
from agentops.models.scenarios import build_scenarios
from agentops.queue import jobs
from agentops.queue.worker import Worker
from agentops.settings import get_settings
from agentops.tools.catalog import native_tools
from agentops.tools.mcp import MCPManager, ServerConfig
from agentops.tools.native.email import EmailIn, send_email
from agentops.tools.registry import ToolRegistry
from agentops.tools.spec import ToolContext, ToolOutput, ToolSpec
from tests.integration.conftest import (
    ROOT,
    SeededUser,
    create_task,
    graph_state_values,
    pending_approvals,
)

pytestmark = pytest.mark.integration

# Every proposal-only tool we ship, with valid arguments. If a new one is added, the
# exhaustiveness assertion below fails until it is listed here (and therefore tested).
PROPOSAL_ARGS: dict[str, dict[str, Any]] = {
    "send_email": {"to": "team@example.com", "subject": "hi", "body": "b"},
    "notes__create_note": {"title": "t", "body": "b"},
    "notes__delete_note": {"note_id": 1},
}


@pytest.fixture
async def notes(tmp_path: Path) -> AsyncIterator[tuple[MCPManager, Path]]:
    db = tmp_path / "notes.db"
    cfg = ServerConfig(
        name="notes",
        transport="stdio",
        expect_name="agentops-notes",
        module="mcp_servers.notes.server",
        args=["--db", str(db)],
        allowed_agents=frozenset({"writing"}),
        tool_agents={"search_notes": frozenset({"research", "writing"})},
        risk={"search_notes": "read", "create_note": "external"},
    )
    manager = MCPManager([cfg], tmp_path)
    yield manager, db
    await manager.close()


def spied(specs: list[ToolSpec], calls: list[str]) -> list[ToolSpec]:
    """Wrap the executor of every tool that must never run from a specialist."""
    out = []
    for s in specs:
        if s.executes_directly:
            out.append(s)
            continue

        async def spy(args: Any, ctx: ToolContext, _name: str = s.name) -> ToolOutput:
            calls.append(_name)
            return ToolOutput(text="EXECUTED")

        out.append(dataclasses.replace(s, fn=spy))
    return out


def ctx(engine: AsyncEngine, tid: uuid.UUID, agent: str) -> ToolContext:
    return ToolContext(
        task_id=tid,
        owner_id=None,
        agent=agent,
        subtask_id="s",
        revision=0,
        step=1,
        workspace=Path("/nonexistent"),
        settings=get_settings(),
        engine=engine,
    )


async def test_every_proposal_only_tool_for_every_allowed_agent(
    engine: AsyncEngine, alice: SeededUser, notes: tuple[MCPManager, Path]
) -> None:
    manager, db = notes
    specs = native_tools(get_settings()) + await manager.load_specs()
    gated = {s.name for s in specs if not s.executes_directly}
    assert gated == set(PROPOSAL_ARGS), "a proposal-only tool is missing from PROPOSAL_ARGS"
    calls: list[str] = []
    reg = ToolRegistry(spied(specs, calls))
    tid = await create_task(engine, alice)
    checked = 0
    for spec in specs:
        if spec.executes_directly:
            continue
        for agent in sorted(spec.allowed_agents):
            inv = await reg.invoke(spec.name, PROPOSAL_ARGS[spec.name], ctx(engine, tid, agent))
            assert inv.status == "proposed", (spec.name, agent, inv.content)
            assert inv.proposal is not None and inv.proposal.tool == spec.name
            checked += 1
        for agent in sorted({"research", "analysis", "code", "writing"} - spec.allowed_agents):
            inv = await reg.invoke(spec.name, PROPOSAL_ARGS[spec.name], ctx(engine, tid, agent))
            assert inv.status == "denied"
    assert checked >= 3 and calls == []
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


async def test_specialist_run_records_proposals_and_executes_none(
    engine: AsyncEngine, alice: SeededUser, notes: tuple[MCPManager, Path], tmp_path: Path
) -> None:
    manager, db = notes
    calls: list[str] = []
    specs = spied(native_tools(get_settings()) + await manager.load_specs(), calls)
    script = build_scenarios()
    plan = ScriptedTurn(
        structured={
            "subtasks": [
                {
                    "id": "w",
                    "description": "write and share",
                    "specialist": "writing",
                    "depends_on": [],
                    "expected_output": "summary",
                    "complexity": "low",
                }
            ],
            "confidence": 0.9,
            "rationale": "t",
        }
    )
    script.add("supervisor", "t", plan, purpose="plan")
    script.add(
        "writing",
        "t",
        ScriptedTurn(
            tool_calls=[
                {"name": "send_email", "args": PROPOSAL_ARGS["send_email"]},
                {"name": "notes__create_note", "args": PROPOSAL_ARGS["notes__create_note"]},
                {"name": "notes__delete_note", "args": PROPOSAL_ARGS["notes__delete_note"]},
                {"name": "workspace_write", "args": {"path": "out.md", "content": "local"}},
            ]
        ),
        ScriptedTurn(content="Summary written; actions proposed."),
        purpose="act",
    )
    settings = get_settings().model_copy(update={"workspace_root": tmp_path / "ws"})
    tid = await create_task(engine, alice, "share it", graph="main")
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(settings.libpq_dsn, max_size=4) as saver:
        w = Worker(
            settings,
            engine,
            saver,
            "w",
            chat_factory=fake_factory(script, "t"),
            tools=ToolRegistry(specs),
        )
        job = await jobs.claim(engine, "w", settings.job_lease_seconds)
        assert job is not None
        await w.process(job)
    async with engine.connect() as c:
        status, _result = (
            await c.execute(text("SELECT status::text, result FROM tasks WHERE id=:t"), {"t": tid})
        ).one()
        logged = [
            tuple(r)
            for r in await c.execute(
                text("SELECT tool, status FROM tool_calls WHERE task_id=:t ORDER BY id"),
                {"t": tid},
            )
        ]
    # Phase 4: the run now pauses at action_gate for the first proposal; nothing executed
    assert status == "awaiting_approval"
    assert calls == []  # no proposal-only executor ran
    values = await graph_state_values(tid)
    assert sorted(a.tool for a in values["proposed_actions"].values()) == sorted(
        ["send_email", "notes__create_note", "notes__delete_note"]
    )
    assert logged == [
        ("send_email", "proposed"),
        ("notes__create_note", "proposed"),
        ("notes__delete_note", "proposed"),
        ("workspace_write", "ok"),  # inside the task workspace: runs
    ]
    assert (settings.workspace_root / str(tid) / "out.md").read_text() == "local"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_tool_functions_are_only_called_by_the_registry() -> None:
    """Static check: no module other than tools/registry.py calls a ToolSpec's `fn`."""
    offenders = []
    for path in (ROOT / "agentops").rglob("*.py"):
        if path.name == "registry.py" and path.parent.name == "tools":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fn"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


async def test_send_email_executor_delivers_to_mailpit_only(engine: AsyncEngine) -> None:
    """The executor Phase 4 will call after approval. Mailpit captures; nothing leaves."""
    subject = f"agentops test {uuid.uuid4().hex[:8]}"
    out = await send_email(
        EmailIn(to="team@example.com", subject=subject, body="hello"),
        ctx(engine, uuid.uuid4(), "writing"),
        message_id="idem-key-123",
    )
    assert out.data == {"message_id": "<idem-key-123@agentops>"}
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "http://localhost:8025/api/v1/search", params={"query": f'subject:"{subject}"'}
        )
    msgs = r.json()["messages"]
    assert len(msgs) == 1 and msgs[0]["MessageID"] == "idem-key-123@agentops"


@pytest.mark.parametrize(
    "bad",
    [
        {"to": "a@b.co\r\nBcc: x@evil.example", "subject": "s", "body": "b"},
        {"to": "a@b.co, x@evil.example", "subject": "s", "body": "b"},
        {"to": "a@b.co", "subject": "s\r\nBcc: x@evil.example", "body": "b"},
    ],
)
def test_email_header_injection_is_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        EmailIn.model_validate(bad)


async def test_same_action_proposed_in_two_revisions_is_kept_once(
    engine: AsyncEngine, alice: SeededUser, tmp_path: Path
) -> None:
    """Each revision's tools node commits its proposals. The second, identical proposal has
    the same hash, so the reducer keeps one (Phase 4 executes each hash at most once)."""
    script = build_scenarios()
    plan = ScriptedTurn(
        structured={
            "subtasks": [
                {
                    "id": "w",
                    "description": "write",
                    "specialist": "writing",
                    "depends_on": [],
                    "expected_output": "summary",
                    "complexity": "low",
                }
            ],
            "confidence": 0.9,
            "rationale": "t",
        }
    )
    email = {"name": "send_email", "args": PROPOSAL_ARGS["send_email"]}
    script.add("supervisor", "t", plan, purpose="plan")
    for r in ("r0", "r1"):
        script.add(
            "writing",
            "t",
            ScriptedTurn(tool_calls=[email]),
            ScriptedTurn(content=f"draft {r}"),
            purpose=f"act/{r}",
        )
    script.add(
        "reviewer",
        "t",
        ScriptedTurn(
            structured={
                "verdict": "revise",
                "score": 2,
                "issues": ["more"],
                "feedback_for_specialist": "more",
                "unsupported_claims": [],
            }
        ),
        purpose="review/r0",
    )
    settings = get_settings().model_copy(update={"workspace_root": tmp_path / "ws"})
    tid = await create_task(engine, alice, "x", graph="main")
    await jobs.enqueue(engine, "run", thread_id=tid)
    async with open_checkpointer(settings.libpq_dsn, max_size=4) as saver:
        w = Worker(settings, engine, saver, "w", chat_factory=fake_factory(script, "t"))
        job = await jobs.claim(engine, "w", settings.job_lease_seconds)
        assert job is not None
        await w.process(job)
    async with engine.connect() as c:
        status, _result = (
            await c.execute(text("SELECT status::text, result FROM tasks WHERE id=:t"), {"t": tid})
        ).one()
        logged = (
            await c.execute(
                text(
                    "SELECT array_agg(revision ORDER BY id) FROM tool_calls "
                    "WHERE task_id=:t AND status='proposed'"
                ),
                {"t": tid},
            )
        ).scalar_one()
    assert status == "awaiting_approval"  # Phase 4: paused at action_gate
    assert logged == [0, 1]  # proposed in both revisions...
    [proposal] = (await graph_state_values(tid))["proposed_actions"].values()  # ...kept once
    assert proposal.revision == 0  # first proposal wins
    assert [k for k, _ in await pending_approvals(engine, tid)] == ["approve_action"]
