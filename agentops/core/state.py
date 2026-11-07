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

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class PlanConfidence(_Frozen):
    """effective = min(self_reported, structural) (case study 5.3; ADR-036)."""

    self_reported: float
    structural: float
    effective: float
    high: int
    medium: int
    depth: int
    memory_similarity: float
    threshold: float

    @property
    def needs_approval(self) -> bool:
        return self.effective < self.threshold


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


class ToolLogEntry(_Frozen):
    """One tool call in a specialist attempt, as the reviewer sees it (truncated)."""

    step: int
    tool: str
    status: str
    args: dict[str, Any]
    preview: str


class AttemptRecord(_Frozen):
    """How one specialist attempt ended. Each one counts toward MAX_REVISIONS."""

    revision: int
    outcome: Literal["reviewed", "step_cap"]
    draft: str | None = None
    verdict: ReviewVerdict | None = None
    note: str | None = None  # what the specialist is told before its next attempt


class ScheduleDecision(_Frozen):
    kind: Literal["dispatch", "escalate", "fail", "all_accepted", "partial"]
    subtask_ids: list[str] = Field(default_factory=list)
    # subtasks newly marked blocked by this decision (written into results by `schedule`)
    blocked: dict[str, str] = Field(default_factory=dict)  # id -> reason
    reason: str | None = None


# ---- human decisions (resume values; validated by interrupt(response_schema=...) and the API)
DecisionAction = Literal["approve", "edit", "reject", "take_over"]


class _Decision(_Frozen):
    note: str | None = Field(default=None, max_length=2000)


def _needs(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


class EscalationDecision(_Decision):
    """escalation_gate for needs_human subtasks."""

    action: DecisionAction
    edits: dict[str, str] = Field(default_factory=dict)  # edit: subtask_id -> replacement output
    deliverable: str | None = Field(default=None, max_length=50_000)  # take_over

    @model_validator(mode="after")
    def _check(self) -> EscalationDecision:
        if self.action == "edit":
            _needs(bool(self.edits), "edit needs `edits`")
        if self.action == "take_over":
            _needs(bool(self.deliverable), "take_over needs `deliverable`")
        return self


class PlanDecision(_Decision):
    """plan_gate (approve_plan)."""

    action: DecisionAction
    plan: Plan | None = None  # edit: the replacement plan (re-validated as a DAG)
    deliverable: str | None = Field(default=None, max_length=50_000)  # take_over

    @model_validator(mode="after")
    def _check(self) -> PlanDecision:
        if self.action == "edit":
            _needs(self.plan is not None, "edit needs `plan`")
        if self.action == "take_over":
            _needs(bool(self.deliverable), "take_over needs `deliverable`")
        return self


class ActionDecision(_Decision):
    """action_gate (approve_action). take_over = a human did it themselves (not executed)."""

    action: DecisionAction
    args: dict[str, Any] | None = None  # edit: replacement args (re-validated by the tool)

    @model_validator(mode="after")
    def _check(self) -> ActionDecision:
        if self.action == "edit":
            _needs(self.args is not None, "edit needs `args`")
        return self


class FinalDecision(_Decision):
    """escalation_gate for a deliverable whose final review scored below threshold."""

    action: DecisionAction
    deliverable: str | None = Field(default=None, max_length=50_000)  # edit / take_over

    @model_validator(mode="after")
    def _check(self) -> FinalDecision:
        if self.action in {"edit", "take_over"}:
            _needs(bool(self.deliverable), f"{self.action} needs `deliverable`")
        return self


class BudgetDecision(_Decision):
    """Worker-level pause when the ledger refuses a call (not a graph interrupt). approve raises
    one or more limits; each given limit must be above the current one."""

    action: Literal["approve", "reject"]
    max_usd: float | None = Field(default=None, gt=0)
    max_llm_calls: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> BudgetDecision:
        if self.action == "approve":
            _needs(
                any(v is not None for v in (self.max_usd, self.max_llm_calls, self.max_tokens)),
                "approve needs at least one raised limit (max_usd, max_llm_calls, max_tokens)",
            )
        return self


class RecursionDecision(_Decision):
    """Worker-level pause when the graph hits its recursion limit."""

    action: Literal["approve", "reject", "take_over"]
    deliverable: str | None = Field(default=None, max_length=50_000)

    @model_validator(mode="after")
    def _check(self) -> RecursionDecision:
        if self.action == "take_over":
            _needs(bool(self.deliverable), "take_over needs `deliverable`")
        return self


class SideEffectResolution(_Decision):
    """Non-blocking inbox item for an action whose outcome is unknown. Records only."""

    action: Literal["confirmed_done", "not_done"]


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


class ActionDecisionRecord(_Frozen):
    hash: str
    key: str  # approval key
    decision: ActionDecision


class ActionOutcome(_Frozen):
    """What happened to one proposed action (ADR-004, ADR-037)."""

    hash: str
    tool: str
    status: Literal["done", "failed", "unknown", "rejected", "human_done", "not_considered"]
    idempotency_key: str | None = None
    detail: str | None = None


def merge_by_key(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    return {**(left or {}), **(right or {})}


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
    plan_confidence: PlanConfidence | None
    plan_decision: PlanDecision | None
    memory_similarity: float  # best recalled-memory similarity (set by recall; 0 if none)
    results: Annotated[dict[str, SubtaskResult], merge_results]
    reviews: Annotated[list[Review], operator.add]
    wave: int
    decision: ScheduleDecision | None
    escalations: Annotated[list[Escalation], operator.add]
    proposed_actions: Annotated[dict[str, ProposedAction], merge_proposals]
    action_decisions: Annotated[dict[str, ActionDecisionRecord], merge_by_key]
    action_outcomes: Annotated[dict[str, ActionOutcome], merge_by_key]
    failure_reason: str | None
    final: str | None
    partial: bool
    final_review: ReviewVerdict | None
    final_review_failed: bool
    last_gate_action: str | None
    final_decisions: Annotated[list[FinalDecision], operator.add]
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
    revision: int
    history: list[AttemptRecord]  # finished attempts, oldest first
    local_reviews: list[Review]
    # the current attempt (reset when a new attempt starts)
    messages: list[BaseMessage]
    step: int  # tool calls made in this attempt
    tool_log: list[ToolLogEntry]
    draft: str | None
    attempt_outcome: Literal["reviewed", "step_cap"] | None
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
    PlanConfidence,
    PlanRejection,
    ReviewVerdict,
    Review,
    SubtaskResult,
    ScheduleDecision,
    EscalationDecision,
    PlanDecision,
    ActionDecision,
    FinalDecision,
    BudgetDecision,
    RecursionDecision,
    SideEffectResolution,
    Escalation,
    ProposedAction,
    ActionDecisionRecord,
    ActionOutcome,
    ToolLogEntry,
    AttemptRecord,
    Outcome,
)
