"""Tool specification and per-call context (ADR-008, ADR-030).

Risk says what a tool can affect; scope says where. Together they decide whether a specialist
may run it directly (ADR-003):

| risk        | scope                       | specialist call ...                          |
|-------------|-----------------------------|----------------------------------------------|
| read        | any                         | executes                                     |
| write       | task_workspace or sandbox   | executes (affects nothing outside the task)  |
| write       | outside                     | becomes a ProposedAction, never executed     |
| external    | any                         | becomes a ProposedAction, never executed     |
| destructive | any                         | becomes a ProposedAction, never executed     |
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from agentops.settings import Settings

Risk = Literal["read", "write", "external", "destructive"]
Scope = Literal["none", "task_workspace", "sandbox", "outside"]
Agent = Literal["research", "analysis", "writing", "code"]
ALL_SPECIALISTS: frozenset[str] = frozenset({"research", "analysis", "writing", "code"})


class ToolInput(BaseModel):
    """Base for tool input models: unknown arguments are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


@dataclass(frozen=True)
class ToolContext:
    task_id: uuid.UUID
    owner_id: uuid.UUID | None
    agent: str
    subtask_id: str
    revision: int
    step: int
    workspace: Path  # this task's workspace dir, as the worker sees it
    settings: Settings
    engine: AsyncEngine


@dataclass(frozen=True)
class ToolOutput:
    """What a tool function returns. `text` goes to the model (framed as untrusted data)."""

    text: str
    data: dict[str, Any] | None = None


ToolFn = Callable[[Any, ToolContext], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    fn: ToolFn
    allowed_agents: frozenset[str]
    risk: Risk
    scope: Scope = "none"
    timeout_s: float = 30.0
    rate_per_min: int = 30
    source: Literal["native", "mcp"] = "native"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def executes_directly(self) -> bool:
        if self.risk == "read":
            return True
        return self.risk == "write" and self.scope in {"task_workspace", "sandbox"}

    def model_schema(self) -> dict[str, Any]:
        """OpenAI-style function tool; both the fake and Anthropic adapters accept it."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }
