"""MCP tools (ADR-008, ADR-032): allowlisted servers, wrapped by our ToolRegistry.

- Only servers listed in config/mcp_servers.toml (and enabled via MCP_SERVERS) are ever
  started or contacted. A server must also report the expected name at initialize.
- Tool names are namespaced `server__tool`.
- Risk defaults to `destructive` unless the config overrides it per tool. Anything other than
  `read` is proposal-only for specialists (ADR-003), so MCP tools can't act on the world here.
- Descriptions come from an external process: control characters stripped, length capped,
  and labelled as unverified before the model sees them.
- Input schemas (JSON Schema) are converted to Pydantic models with extra="forbid" for a
  supported subset (object of primitives, enums, arrays of primitives, common constraints).
  A tool with anything else is skipped with a warning, never loaded half-validated.

stdio servers (Phase 3 change 3):
- explicit minimal environment. mcp's stdio_client always merges six variables from the
  parent (HOME, LOGNAME, PATH, SHELL, TERM, USER); we pass our own value for every one of
  them, so the child sees only values chosen here. No API keys, DB URLs or other worker env.
- startup timeout (spawn + initialize), per-call timeout, and restart on crash: a dead
  connection is restarted on the next call; read-only calls are retried once after a restart.

Built on the SDK v2 client (mcp.client.stdio / mcp.client.streamable_http / ClientSession),
verified against the installed mcp 2.3.0 on 2026-11-07.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys
import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError as SdkMCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT
from pydantic import Field, create_model

from agentops.settings import Settings
from agentops.tools.framing import sanitize
from agentops.tools.spec import Risk, ToolContext, ToolInput, ToolOutput, ToolSpec

log = logging.getLogger(__name__)

NAMESPACE_SEP = "__"
MAX_DESCRIPTION = 300
MAX_RESTARTS = 3  # consecutive restarts without a successful call -> give up


class MCPError(RuntimeError):
    pass


class MCPStartupError(MCPError):
    pass


class UnsupportedSchemaError(ValueError):
    pass


# ---- config --------------------------------------------------------------------------------
@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: Literal["stdio", "streamable_http"]
    expect_name: str | None = None  # serverInfo.name the server must report
    module: str | None = None  # stdio: python -m <module>
    command: list[str] = field(default_factory=list)  # stdio: explicit argv (instead of module)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # literal extra vars, never from worker env
    url: str | None = None  # streamable_http
    allowed_agents: frozenset[str] = frozenset()
    tool_agents: dict[str, frozenset[str]] = field(default_factory=dict)
    risk: dict[str, Risk] = field(default_factory=dict)
    startup_timeout_s: float = 10.0
    call_timeout_s: float = 15.0


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def load_config(path: Path, enabled: list[str], *, data_dir: Path) -> list[ServerConfig]:
    """Read the allowlist; return configs for the enabled names only (unknown names error)."""
    raw = tomllib.loads(path.read_text())
    servers: dict[str, dict[str, Any]] = raw.get("servers", {})
    unknown = sorted(set(enabled) - set(servers))
    if unknown:
        raise MCPError(f"MCP_SERVERS names not in the allowlist {path}: {unknown}")
    out = []
    for name in enabled:
        s = servers[name]
        if not _NAME_RE.match(name):
            raise MCPError(f"invalid MCP server name {name!r}")
        fmt = {"data_dir": str(data_dir / name), "python": sys.executable}
        out.append(
            ServerConfig(
                name=name,
                transport=s["transport"],
                expect_name=s.get("expect_name"),
                module=s.get("module"),
                command=[c.format(**fmt) for c in s.get("command", [])],
                args=[a.format(**fmt) for a in s.get("args", [])],
                env={k: str(v) for k, v in s.get("env", {}).items()},
                url=s.get("url"),
                allowed_agents=frozenset(s.get("allowed_agents", [])),
                tool_agents={k: frozenset(v) for k, v in s.get("tool_agents", {}).items()},
                risk=dict(s.get("risk", {})),
                startup_timeout_s=float(s.get("startup_timeout_s", 10)),
                call_timeout_s=float(s.get("call_timeout_s", 15)),
            )
        )
    return out


# ---- minimal environment for stdio servers -------------------------------------------------
def minimal_env(cfg: ServerConfig, data_dir: Path) -> dict[str, str]:
    """Every variable the child will see. Overrides all six that mcp's stdio_client would
    otherwise copy from the worker (HOME, LOGNAME, PATH, SHELL, TERM, USER)."""
    base = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(data_dir),
        "USER": "mcp",
        "LOGNAME": "mcp",
        "SHELL": "/usr/sbin/nologin",
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return base | cfg.env


# ---- JSON Schema -> Pydantic ---------------------------------------------------------------
_PRIMITIVES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}
_CONSTRAINTS = {
    "minLength": "min_length",
    "maxLength": "max_length",
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minItems": "min_length",
    "maxItems": "max_length",
}
_IGNORED = {"title", "description", "default", "examples"}


def _field_type(prop: dict[str, Any], where: str) -> Any:
    if "enum" in prop:
        values = prop["enum"]
        if not values or not all(isinstance(v, str) for v in values):
            raise UnsupportedSchemaError(f"{where}: only string enums are supported")
        return Literal[tuple(values)]
    t = prop.get("type")
    if t in _PRIMITIVES:
        return _PRIMITIVES[t]
    if t == "array":
        items = prop.get("items", {})
        if items.get("type") not in _PRIMITIVES:
            raise UnsupportedSchemaError(f"{where}: arrays of {items.get('type')} not supported")
        item_type: Any = _PRIMITIVES[items["type"]]
        return list[item_type]  # built at runtime from the schema
    raise UnsupportedSchemaError(f"{where}: type {t!r} not supported")


def schema_to_model(tool_name: str, schema: dict[str, Any]) -> type[ToolInput]:
    if schema.get("type") != "object":
        raise UnsupportedSchemaError("input schema must be an object")
    for key in ("$ref", "$defs", "anyOf", "oneOf", "allOf", "patternProperties"):
        if key in schema:
            raise UnsupportedSchemaError(f"{key} is not supported")
    props: dict[str, dict[str, Any]] = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for pname, prop in props.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", pname):
            raise UnsupportedSchemaError(f"bad property name {pname!r}")
        unknown = set(prop) - _IGNORED - set(_CONSTRAINTS) - {"type", "enum", "items"}
        if unknown:
            raise UnsupportedSchemaError(f"{pname}: unsupported keywords {sorted(unknown)}")
        typ = _field_type(prop, pname)
        kw = {_CONSTRAINTS[k]: v for k, v in prop.items() if k in _CONSTRAINTS}
        if "description" in prop:
            kw["description"] = sanitize(str(prop["description"]))[:200]
        if pname in required:
            fields[pname] = (typ, Field(**kw))
        else:
            fields[pname] = (typ | None, Field(default=prop.get("default"), **kw))
    model: type[ToolInput] = create_model(f"MCP_{tool_name}", __base__=ToolInput, **fields)
    return model


def untrusted_description(server: str, text: str | None) -> str:
    body = " ".join(sanitize(text or "").split())[:MAX_DESCRIPTION]
    return f"[external MCP tool from '{server}'; description not verified] {body}"


# ---- connection supervisor -----------------------------------------------------------------
def root_cause(exc: BaseException) -> BaseException:
    """anyio task groups wrap errors in exception groups; report the first real one."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class MCPConnection:
    """One live session to one server, owned by a supervisor task (the SDK's transports are
    anyio context managers and must be entered and exited in the same task)."""

    def __init__(self, cfg: ServerConfig, data_dir: Path) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.starts = 0
        self._failures = 0

    @property
    def alive(self) -> bool:
        return self._session is not None and self._task is not None and not self._task.done()

    @contextlib.asynccontextmanager
    async def _transport(self) -> AsyncIterator[Any]:
        if self.cfg.transport == "stdio":
            if self.cfg.module:
                argv = [sys.executable, "-I", "-m", self.cfg.module, *self.cfg.args]
            elif self.cfg.command:
                argv = [*self.cfg.command, *self.cfg.args]
            else:
                raise MCPError(f"{self.cfg.name}: stdio server needs `module` or `command`")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            params = StdioServerParameters(
                command=argv[0],
                args=argv[1:],
                env=minimal_env(self.cfg, self.data_dir),
                cwd=str(self.data_dir),
            )
            async with stdio_client(params) as streams:
                yield streams
        else:
            if not self.cfg.url:
                raise MCPError(f"{self.cfg.name}: streamable_http server needs `url`")
            async with streamable_http_client(self.cfg.url) as streams:
                yield streams

    async def _supervise(self, ready: asyncio.Future[None]) -> None:
        try:
            async with self._transport() as (read, write), ClientSession(read, write) as session:
                init = await session.initialize()
                reported = init.server_info.name
                if self.cfg.expect_name and reported != self.cfg.expect_name:
                    raise MCPStartupError(
                        f"{self.cfg.name}: server reported name {reported!r}, "
                        f"expected {self.cfg.expect_name!r}"
                    )
                self._session = session
                ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:
            cause = root_cause(exc)
            if not ready.done():
                if isinstance(cause, Exception):
                    ready.set_exception(cause)
                else:  # cancelled (e.g. by a startup timeout): nobody is waiting for a result
                    ready.cancel()
            if not isinstance(exc, Exception):
                raise
            log.warning("MCP server %s stopped: %s", self.cfg.name, cause)
        finally:
            self._session = None

    async def start(self) -> None:
        self._stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._supervise(ready), name=f"mcp:{self.cfg.name}")
        self.starts += 1
        try:
            await asyncio.wait_for(asyncio.shield(ready), self.cfg.startup_timeout_s)
        except TimeoutError:
            await self.close()
            raise MCPStartupError(
                f"{self.cfg.name}: no MCP handshake within {self.cfg.startup_timeout_s:.0f}s"
            ) from None
        except Exception as exc:
            await self.close()
            if isinstance(exc, MCPStartupError):
                raise
            raise MCPStartupError(f"{self.cfg.name}: failed to start: {root_cause(exc)}") from exc

    async def close(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except (TimeoutError, Exception):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._session = None

    async def _ensure(self) -> ClientSession:
        async with self._lock:
            if not self.alive:
                if self.starts and self._failures >= MAX_RESTARTS:
                    raise MCPError(f"{self.cfg.name}: gave up after {MAX_RESTARTS} restarts")
                if self.starts:
                    log.warning("restarting MCP server %s", self.cfg.name)
                    self._failures += 1
                await self.close()
                await self.start()
            assert self._session is not None
            return self._session

    async def list_tools(self) -> list[Any]:
        session = await self._ensure()
        result = await asyncio.wait_for(session.list_tools(), self.cfg.call_timeout_s)
        return list(result.tools)

    async def call(self, tool: str, args: dict[str, Any], *, retry_safe: bool) -> Any:
        attempts = 2 if retry_safe else 1
        for attempt in range(1, attempts + 1):
            session = await self._ensure()
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool, args, read_timeout_seconds=self.cfg.call_timeout_s),
                    self.cfg.call_timeout_s + 1,
                )
            except TimeoutError:
                raise MCPError(
                    f"{self.cfg.name}__{tool} timed out after {self.cfg.call_timeout_s:.0f}s"
                ) from None
            except SdkMCPError as exc:
                if exc.code == CONNECTION_CLOSED:  # the server died: restart (and maybe retry)
                    await self.close()
                    if attempt < attempts:
                        continue
                    raise MCPError(f"{self.cfg.name} connection closed") from None
                # Any other error was answered over a live connection: keep the session.
                if exc.code == REQUEST_TIMEOUT:
                    raise MCPError(
                        f"{self.cfg.name}__{tool} timed out after {self.cfg.call_timeout_s:.0f}s"
                    ) from None
                raise MCPError(f"{self.cfg.name}__{tool}: {exc.message}") from None
            except Exception as exc:
                # transport died (crash, closed pipe, dropped HTTP session): restart next time
                await self.close()
                if attempt < attempts:
                    continue
                raise MCPError(f"{self.cfg.name} connection failed: {root_cause(exc)}") from exc
            self._failures = 0
            return result
        raise MCPError("unreachable")


# ---- registry adapter ----------------------------------------------------------------------
def _result_text(result: Any) -> str:
    parts = []
    for c in result.content:
        text = getattr(c, "text", None)
        parts.append(text if text is not None else f"[{getattr(c, 'type', 'content')}]")
    return "\n".join(parts)


def spec_for(conn: MCPConnection, tool: Any) -> ToolSpec:
    cfg = conn.cfg
    name = f"{cfg.name}{NAMESPACE_SEP}{tool.name}"
    risk: Risk = cfg.risk.get(tool.name, "destructive")
    model = schema_to_model(name, dict(tool.input_schema or {"type": "object"}))
    retry_safe = risk == "read"

    async def run(args: ToolInput, ctx: ToolContext) -> ToolOutput:
        result = await conn.call(
            tool.name, args.model_dump(exclude_none=True), retry_safe=retry_safe
        )
        text = _result_text(result)
        if result.is_error:
            raise MCPError(text[:500] or "tool reported an error")
        return ToolOutput(text=text)

    return ToolSpec(
        name=name,
        description=untrusted_description(cfg.name, tool.description),
        input_model=model,
        fn=run,
        allowed_agents=cfg.tool_agents.get(tool.name, cfg.allowed_agents),
        risk=risk,
        scope="none" if risk == "read" else "outside",
        timeout_s=cfg.call_timeout_s * 2 + 2,  # covers one restart + retry
        rate_per_min=30,
        source="mcp",
        meta={"server": cfg.name, "tool": tool.name},
    )


class MCPManager:
    """Owns the connections for a worker process."""

    def __init__(self, configs: list[ServerConfig], data_root: Path) -> None:
        self.connections = {c.name: MCPConnection(c, data_root / c.name) for c in configs}
        self.skipped: dict[str, str] = {}

    async def load_specs(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for conn in self.connections.values():
            try:
                tools = await conn.list_tools()
            except MCPError as exc:
                # One broken optional server must not stop the worker: its tools are absent.
                self.skipped[conn.cfg.name] = f"unavailable: {exc}"
                log.error(
                    "MCP server %s unavailable, its tools are not loaded: %s", conn.cfg.name, exc
                )
                continue
            for tool in tools:
                try:
                    specs.append(spec_for(conn, tool))
                except UnsupportedSchemaError as exc:
                    key = f"{conn.cfg.name}{NAMESPACE_SEP}{tool.name}"
                    self.skipped[key] = str(exc)
                    log.warning("skipping MCP tool %s: %s", key, exc)
        return specs

    async def close(self) -> None:
        for conn in self.connections.values():
            await conn.close()


def manager_from_settings(settings: Settings) -> MCPManager | None:
    if not settings.mcp_servers:
        return None
    data_root = settings.mcp_data_dir.resolve()
    configs = load_config(settings.mcp_config_path, settings.mcp_servers, data_dir=data_root)
    return MCPManager(configs, data_root)
