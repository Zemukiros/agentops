"""Builds the tool registry for a worker process: native tools + allowlisted MCP tools."""

from __future__ import annotations

from agentops.settings import Settings
from agentops.tools.mcp import MCPManager
from agentops.tools.native.python_tool import PYTHON_TOOLS
from agentops.tools.native.sql import SQL_TOOLS
from agentops.tools.native.web import WEB_TOOLS
from agentops.tools.native.workspace import WORKSPACE_TOOLS
from agentops.tools.registry import ToolRegistry
from agentops.tools.spec import ToolSpec


def native_tools(settings: Settings) -> list[ToolSpec]:
    return [*WORKSPACE_TOOLS, *SQL_TOOLS, *WEB_TOOLS, *PYTHON_TOOLS]


def build_registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(native_tools(settings))


async def build_registry_with_mcp(settings: Settings, manager: MCPManager | None) -> ToolRegistry:
    specs = native_tools(settings)
    if manager is not None:
        specs += await manager.load_specs()
    return ToolRegistry(specs)
