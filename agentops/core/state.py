"""Graph state types (DECISIONS.md "Corrected workflow graph", ADR-001/002).

Pydantic models are used for values the model produces or the UI reads. They are stored in
checkpoints, so every class here is registered in `CHECKPOINT_TYPES` (the checkpoint
serializer's msgpack allowlist, ADR-028). Add new state classes there too.
"""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from agentops.models.client import ModelClient
    from agentops.tools.registry import ToolRegistry

Specialist = Literal["research", "analysis", "writing", "code"]
TerminalStatus = Literal[
    "completed", "completed_partial", "failed", "rejected", "cancelled", "budget_exceeded"
]
SubtaskStatus = Literal["accepted", "failed", "needs_human", "blocked"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- produced by the supervisor (structured output) ---------------------------------------
class Subtask(_Frozen):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$", description="short slug, e.g. 'draft'")
    description: str = Field(min_length=1, max_length=2000)
    specialist: Specialist
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str = Field(min_length=1, max_length=1000)
    complexity: Literal["low", "medium", "high"] = "medium"


class Plan(_Frozen):
    subtasks: list[Subtask] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=2000)


class PlanRejection(_Frozen):
    """A plan that failed validation, kept so the re-plan prompt can show it."""

    attempt: int
    plan_json: str
    errors: list[str]


# ---- produced by the reviewer (structured output) -----------------------------------------
class ReviewVerdict(_Frozen):
    verdict: Literal["accept", "revise", "escalate"]
    score: int = Field(ge=1, le=5)
    issues: list[str] = Field(default_factory=list)
    feedback_for_specialist: str | None = None
    unsupported_claims: list[str] = Field(default_factory=list)


class Review(_Frozen):
    """A ReviewVerdict as recorded in state, tied to its subtask and revision."""

    subtask_id: str
    revision: int
    verdict: ReviewVerdict


# ---- terminal results and outcomes ---------------------------------------------------------
class SubtaskResult(_Frozen):
    """Only terminal statuses (ADR-002). Retries live in the subgraph's own state.
    `blocked` is set by the scheduler (never by a subtask_worker) for subtasks that depend,
    directly or transitively, on a failed or blocked subtask (ADR-029)."""

    subtask_id: str
    status: SubtaskStatus
    output: str | None = None  # last draft (also for needs_human, so a human can judge it)
    revisions: int = 0
    reason: str | None = None
    human_override: bool = False


class ScheduleDecision(_Frozen):
    kind: Literal["dispatch", "escalate", "fail", "all_accepted", "partial"]
    subtask_ids: list[str] = Field(default_factory=list)
    # subtasks newly marked blocked by this decision (written into results by `schedule`)
    blocked: dict[str, str] = Field(default_factory=dict)  # id -> reason
    reason: str | None = None


class EscalationDecision(_Frozen):
    """Human answer to an escalation (resume value of escalation_gate's interrupt)."""

    action: Literal["approve", "edit", "reject"]
    # edit: subtask_id -> replacement output
    edits: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


class Escalation(_Frozen):
    round: int
    subtask_ids: list[str]
    decision: EscalationDecision


class ProposedAction(_Frozen):
    """A write/external/destructive tool call outside the task workspace. Never executed by a
    specialist (ADR-003); stored for approval and execution in Phase 4.

    `hash` covers task, tool and canonical args only, so a node re-run after a crash, or a
    later revision proposing the same thing, yields the same hash and the reducer keeps one.
    """

    hash: str
    tool: str
    args: dict[str, Any]
    risk: Literal["write", "external", "destructive"]
    agent: str
    subtask_id: str
    revision: int
    rationale: str | None = None


def merge_proposals(
    left: dict[str, ProposedAction] | None, right: dict[str, ProposedAction] | None
) -> dict[str, ProposedAction]:
    """Reducer keyed by action hash; the first proposal of an action wins (ADR-030)."""
    out = dict(left or {})
    for h, action in (right or {}).items():
        out.setdefault(h, action)
    return out


class Outcome(_Frozen):
    status: TerminalStatus
    reason: str | None = None


def merge_results(
    left: dict[str, SubtaskResult] | None, right: dict[str, SubtaskResult] | None
) -> dict[str, SubtaskResult]:
    """Reducer for parallel branches: each branch writes its own subtask id. A later write for
    the same id (escalation_gate overriding needs_human) wins."""
    return {**(left or {}), **(right or {})}


class TaskState(TypedDict, total=False):
    task_id: str
    request: str
    plan: Plan | None
    plan_attempts: int
    plan_rejections: list[PlanRejection]
    results: Annotated[dict[str, SubtaskResult], merge_results]
    reviews: Annotated[list[Review], operator.add]
    wave: int
    decision: ScheduleDecision | None
    escalations: Annotated[list[Escalation], operator.add]
    proposed_actions: Annotated[dict[str, ProposedAction], merge_proposals]
    failure_reason: str | None
    final: str | None
    partial: bool
    final_review: ReviewVerdict | None
    outcome: Outcome | None


class SubtaskInput(TypedDict):
    """What `schedule` sends to one subtask_worker instance."""

    task_id: str
    request: str
    subtask: Subtask
    context: dict[str, str]  # dependency id -> accepted output
    wave: int


class SubtaskOutput(TypedDict):
    """The only keys a subtask_worker writes back to the parent (no private keys leak)."""

    results: dict[str, SubtaskResult]
    reviews: list[Review]
    proposed_actions: dict[str, ProposedAction]


class SubtaskState(SubtaskInput, total=False):
    results: dict[str, SubtaskResult]
    reviews: list[Review]
    proposed_actions: Annotated[dict[str, ProposedAction], merge_proposals]
    drafts: list[str]
    revision: int
    local_reviews: list[Review]
    failed_reason: str | None


# ---- Phase 1 durability probe --------------------------------------------------------------
class DemoState(TypedDict):
    request: str
    log: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class RunContext:
    """Per-run dependencies passed via LangGraph's runtime context (not checkpointed, ADR-021)."""

    task_id: uuid.UUID
    engine: AsyncEngine
    model: ModelClient | None = None  # None only for the demo graph
    tools: ToolRegistry | None = None  # None only for the demo graph
    owner_id: uuid.UUID | None = None  # rate-limit key; set by the worker


CHECKPOINT_TYPES: tuple[type[BaseModel], ...] = (
    Subtask,
    Plan,
    PlanRejection,
    ReviewVerdict,
    Review,
    SubtaskResult,
    ScheduleDecision,
    EscalationDecision,
    Escalation,
    ProposedAction,
    Outcome,
)
