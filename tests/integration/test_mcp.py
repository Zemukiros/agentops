"""MCP (ADR-032, Phase 3 step 6) with real server processes: our notes server over stdio
AND streamable HTTP, plus a test server that exposes its environment, crashes and stalls."""

import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentops.settings import get_settings
from agentops.tools.mcp import (
    MCPConnection,
    MCPError,
    MCPManager,
    MCPStartupError,
    ServerConfig,
    load_config,
)
from agentops.tools.registry import ToolRegistry
from agentops.tools.spec import ToolContext
from tests.integration.conftest import ROOT, SeededUser, create_task

pytestmark = pytest.mark.integration

FIXTURE_SERVER = str(ROOT / "tests" / "fixtures" / "mcp_test_server.py")
NOTES_RISK = {"search_notes": "read", "create_note": "external"}
NOTES_AGENTS = {"search_notes": frozenset({"research", "writing"})}


def notes_stdio(tmp: Path) -> ServerConfig:
    return ServerConfig(
        name="notes",
        transport="stdio",
        expect_name="agentops-notes",
        module="mcp_servers.notes.server",
        args=["--db", str(tmp / "notes.db")],
        allowed_agents=frozenset({"writing"}),
        tool_agents=NOTES_AGENTS,
        risk=NOTES_RISK,  # type: ignore[arg-type]
    )


def fixture_server(name: str = "agentops-test", **kw: object) -> ServerConfig:
    return ServerConfig(
        name="t",
        transport="stdio",
        command=[sys.executable, "-I", FIXTURE_SERVER, name],
        allowed_agents=frozenset({"research"}),
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
async def conns() -> AsyncIterator[list[MCPConnection]]:
    opened: list[MCPConnection] = []
    yield opened
    for c in opened:
        await c.close()


def open_conn(conns: list[MCPConnection], cfg: ServerConfig, tmp: Path) -> MCPConnection:
    c = MCPConnection(cfg, tmp / cfg.name)
    conns.append(c)
    return c


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def http_notes(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    port = free_port()
    db = tmp_path / "http-notes.db"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "mcp_servers.notes.server",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--db",
            str(db),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("HTTP notes server did not start")
    yield f"http://127.0.0.1:{port}/mcp", db
    proc.terminate()
    proc.wait(10)


def tool_ctx(engine: AsyncEngine, tid: uuid.UUID, agent: str) -> ToolContext:
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


def note_count(db: Path) -> int:
    with sqlite3.connect(db) as c:
        return int(c.execute("SELECT count(*) FROM notes").fetchone()[0])


# ---- both transports, same behaviour -------------------------------------------------------
@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
async def test_notes_over_both_transports(
    transport: str,
    tmp_path: Path,
    conns: list[MCPConnection],
    request: pytest.FixtureRequest,
    engine: AsyncEngine,
    alice: SeededUser,
) -> None:
    if transport == "stdio":
        cfg = notes_stdio(tmp_path)
        db = tmp_path / "notes.db"
    else:
        url, db = request.getfixturevalue("http_notes")
        cfg = ServerConfig(
            name="notes_http",
            transport="streamable_http",
            url=url,
            expect_name="agentops-notes",
            allowed_agents=frozenset({"writing"}),
            tool_agents=NOTES_AGENTS,
            risk=NOTES_RISK,  # type: ignore[arg-type]
        )
    manager = MCPManager([cfg], tmp_path)
    conns.extend(manager.connections.values())
    specs = {s.name: s for s in await manager.load_specs()}
    p = cfg.name
    assert set(specs) == {f"{p}__create_note", f"{p}__search_notes", f"{p}__delete_note"}
    assert specs[f"{p}__search_notes"].risk == "read"
    assert specs[f"{p}__create_note"].risk == "external"
    assert specs[f"{p}__delete_note"].risk == "destructive"  # not overridden -> default
    assert all(s.source == "mcp" for s in specs.values())
    assert specs[f"{p}__search_notes"].description.startswith(
        f"[external MCP tool from '{p}'; description not verified]"
    )

    # seed one note directly through the connection (test setup, bypasses the registry)
    conn = manager.connections[p]
    await conn.call("create_note", {"title": "pgvector", "body": "HNSW index"}, retry_safe=False)
    assert note_count(db) == 1

    tid = await create_task(engine, alice)
    reg = ToolRegistry(specs.values())
    found = await reg.invoke(
        f"{p}__search_notes", {"query": "HNSW"}, tool_ctx(engine, tid, "research")
    )
    assert found.status == "ok" and "pgvector: HNSW index" in found.content
    # writing may *propose* creating a note; nothing is written
    prop = await reg.invoke(
        f"{p}__create_note", {"title": "x", "body": "y"}, tool_ctx(engine, tid, "writing")
    )
    assert prop.status == "proposed" and prop.proposal is not None
    # research can't even propose a delete
    denied = await reg.invoke(
        f"{p}__delete_note", {"note_id": 1}, tool_ctx(engine, tid, "research")
    )
    assert denied.status == "denied"
    bad = await reg.invoke(
        f"{p}__search_notes", {"query": "x", "x": 1}, tool_ctx(engine, tid, "research")
    )
    assert bad.status == "validation_error"
    assert note_count(db) == 1  # the proposal and the denial changed nothing


async def test_http_server_rejects_foreign_host_headers(http_notes: tuple[str, Path]) -> None:
    """Same valid initialize request, two Host headers: the default allowed host is served (200),
    a foreign one gets 421 Misdirected Request from the DNS-rebinding check. (An earlier version
    sent a bare `ping` and accepted any 4xx, which also passed with the protection OFF.)"""
    url, _ = http_notes
    port = url.split(":")[2].split("/")[0]
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    accept = {"Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient() as c:
        ok = await c.post(url, json=init, headers={**accept, "Host": f"127.0.0.1:{port}"})
        bad = await c.post(url, json=init, headers={**accept, "Host": "evil.example"})
    assert (ok.status_code, bad.status_code) == (200, 421)


async def test_http_host_allowlist_matches_the_compose_configuration(tmp_path: Path) -> None:
    """DNS-rebinding protection, configured as in docker-compose.yml (only the service name is
    allowed): that Host is served; the defaults and a foreign Host are refused. A server that
    rejected everything would fail the first assertion."""
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "mcp_servers.notes.server",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--allowed-host",
            f"mcp-notes:{port}",
            "--db",
            str(tmp_path / "n.db"),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.1)
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        accept = "application/json, text/event-stream"

        async def status_for(host: str) -> int:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json=init,
                    headers={"Host": host, "Accept": accept},
                )
            return r.status_code

        assert await status_for(f"mcp-notes:{port}") == 200  # the configured host is served
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", "evil.example", "mcp-notes"):
            assert await status_for(host) == 421, host  # everything else: Misdirected Request
    finally:
        proc.terminate()
        proc.wait(10)


# ---- allowlist -----------------------------------------------------------------------------
def test_only_allowlisted_servers_can_be_enabled(tmp_path: Path) -> None:
    cfg_path = ROOT / "config" / "mcp_servers.toml"
    loaded = load_config(cfg_path, ["notes"], data_dir=tmp_path)
    assert [c.name for c in loaded] == ["notes"]
    assert loaded[0].args == ["--db", str(tmp_path / "notes" / "notes.db")]
    with pytest.raises(MCPError, match=r"not in the allowlist .*\['rogue'\]"):
        load_config(cfg_path, ["notes", "rogue"], data_dir=tmp_path)


async def test_server_must_report_the_expected_name(
    tmp_path: Path, conns: list[MCPConnection]
) -> None:
    c = open_conn(conns, fixture_server(name="impostor", expect_name="agentops-test"), tmp_path)
    with pytest.raises(MCPStartupError, match="reported name 'impostor'"):
        await c.start()


async def test_unsupported_schema_tool_is_skipped_not_loaded(tmp_path: Path) -> None:
    manager = MCPManager([fixture_server()], tmp_path)
    try:
        names = {s.name for s in await manager.load_specs()}
    finally:
        await manager.close()
    assert "t__nested" not in names and "t__nested" in manager.skipped
    assert "t__echo" in names
    shouty = next(
        s
        for s in (await MCPManager([fixture_server()], tmp_path).load_specs())
        if s.name == "t__shouty"
    )
    assert "\x07" not in shouty.description and len(shouty.description) < 400


async def test_unavailable_server_is_skipped_and_reported(tmp_path: Path) -> None:
    dead = ServerConfig(
        name="dead",
        transport="stdio",
        command=[sys.executable, "-I", "-c", "pass"],
        startup_timeout_s=3,
    )
    manager = MCPManager([dead], tmp_path)
    assert await manager.load_specs() == []
    assert manager.skipped["dead"].startswith("unavailable:")
    await manager.close()


# ---- stdio process hygiene (Phase 3 change 3) ----------------------------------------------
async def test_stdio_child_gets_only_the_minimal_environment(
    tmp_path: Path, conns: list[MCPConnection], monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "canary-" + uuid.uuid4().hex
    monkeypatch.setenv("AGENTOPS_CANARY_SECRET", canary)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + canary)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:{canary}@db/x")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-" + canary)
    # the six names mcp's stdio_client copies from the parent: all must be ours, not these
    for k in ("HOME", "LOGNAME", "SHELL", "TERM", "USER"):
        monkeypatch.setenv(k, f"{k.lower()}-{canary}")
    c = open_conn(conns, fixture_server(), tmp_path)
    result = await c.call("env_dump", {}, retry_safe=True)
    env = json.loads(result.content[0].text)
    assert canary not in json.dumps(env)
    assert set(env) <= {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
        "LC_CTYPE",
    }, sorted(env)
    assert env["HOME"] == str(tmp_path / "t") and env["USER"] == "mcp"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert os.environ["USER"] == f"user-{canary}"  # the parent really had the canary


async def test_startup_timeout(tmp_path: Path, conns: list[MCPConnection]) -> None:
    silent = ServerConfig(
        name="silent",
        transport="stdio",
        command=[sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        startup_timeout_s=1.5,
    )
    c = open_conn(conns, silent, tmp_path)
    started = time.monotonic()
    with pytest.raises(MCPStartupError, match="no MCP handshake within 2s"):
        await c.start()
    assert time.monotonic() - started < 10


async def test_call_timeout_then_still_usable(tmp_path: Path, conns: list[MCPConnection]) -> None:
    c = open_conn(conns, fixture_server(call_timeout_s=1), tmp_path)
    with pytest.raises(MCPError, match="timed out after 1s"):
        await c.call("slow", {"seconds": 5}, retry_safe=False)
    r = await c.call("echo", {"text": "still here"}, retry_safe=True)
    assert r.content[0].text == "still here"


async def test_crashed_server_is_restarted(tmp_path: Path, conns: list[MCPConnection]) -> None:
    c = open_conn(conns, fixture_server(), tmp_path)
    assert (await c.call("echo", {"text": "a"}, retry_safe=True)).content[0].text == "a"
    with pytest.raises(MCPError):
        await c.call("crash", {}, retry_safe=False)  # not retried: not a read
    await asyncio.sleep(0.2)
    r = await c.call("echo", {"text": "b"}, retry_safe=True)
    assert r.content[0].text == "b" and c.starts == 2


async def test_read_call_is_retried_once_when_the_server_died_unnoticed(
    tmp_path: Path, conns: list[MCPConnection]
) -> None:
    """The client still thinks the session is alive; the process was SIGKILLed from outside.
    A read call fails once internally, the server is restarted, and the call succeeds."""
    c = open_conn(conns, fixture_server(), tmp_path)
    pid = int((await c.call("pid", {}, retry_safe=True)).content[0].text)
    os.kill(pid, signal.SIGKILL)
    await asyncio.sleep(0.3)
    r = await c.call("echo", {"text": "after"}, retry_safe=True)
    assert r.content[0].text == "after" and c.starts == 2
    new_pid = int((await c.call("pid", {}, retry_safe=True)).content[0].text)
    assert new_pid != pid
