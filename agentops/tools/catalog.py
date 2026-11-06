"""Builds the tool registry for a worker process: native tools (+ MCP tools in Phase 3 step 7)."""

from __future__ import annotations

from agentops.settings import Settings
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
