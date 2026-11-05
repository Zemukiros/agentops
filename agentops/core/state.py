"""Graph state types.

Phase 1 only defines the state of the durability probe graph. The full TaskState (plan,
results with reducers, reviews, escalations, budget) arrives in Phase 2.
"""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass
from typing import Annotated, TypedDict

from sqlalchemy.ext.asyncio import AsyncEngine


class DemoState(TypedDict):
    request: str
    log: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class RunContext:
    """Per-run dependencies passed via LangGraph's runtime context (not checkpointed)."""

    task_id: uuid.UUID
    engine: AsyncEngine
