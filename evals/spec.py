"""Scenario definitions (evals/scenarios/*.yaml). Fixed before the first paid run: scenarios
and rubrics are never edited to make runs pass (results integrity, EVALUATION.md)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SCENARIO_DIR = Path(__file__).parent / "scenarios"

Category = Literal[
    "planning",
    "parallel",
    "tools",
    "reviewer",
    "approval",
    "crash",
    "injection",
    "memory",
    "cost",
    "loops",
]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    """How the harness (acting as the reviewer) answers an approval request."""

    model_config = ConfigDict(extra="forbid")
    kind: str
    tool: str | None = None
    to_domain: str | None = None  # send_email: match the recipient's domain
    decision: dict[str, Any]


# Defaults when no rule matches: a careful reviewer. Plans/drafts/deliverables are accepted
# (so runs finish), actions are REJECTED unless a scenario says otherwise, budgets aren't
# raised, step limits aren't extended.
DEFAULT_DECISIONS: dict[str, dict[str, Any]] = {
    "approve_plan": {"action": "approve"},
    "escalation": {"action": "approve"},
    "final_review": {"action": "approve"},
    "approve_action": {"action": "reject", "note": "eval default: not approved"},
    "budget": {"action": "reject"},
    "recursion": {"action": "reject"},
    "side_effect_review": {"action": "not_done"},
}


class ReviewerCase(BaseModel):
    """Component eval of the reviewer: one draft, labelled clean or with a planted defect."""

    model_config = ConfigDict(extra="forbid")
    request: str
    description: str
    expected_output: str
    specialist: str = "writing"
    context: dict[str, str] = Field(default_factory=dict)
    draft: str
    defect: str | None = None  # None = clean draft (the reviewer should accept)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: Category
    kind: Literal["agentic", "reviewer", "memory_ab"] = "agentic"
    title: str
    request: str | None = None
    prime: list[str] = Field(default_factory=list)  # memory_ab: tasks run first (arm "on")
    setup: list[Literal["sql_poison", "notes_poison"]] = Field(default_factory=list)
    crash_at: str | None = None  # crash scenarios: worker SIGKILLed when it reaches this hook
    budget_usd: float = Field(default=0.30, gt=0, le=1.0)
    max_llm_calls: int | None = None
    expected_subtasks: int = Field(default=1, ge=1, le=6)  # cost estimate only
    approvals: list[Rule] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    rubric: list[str] = Field(default_factory=list)
    reviewer_case: ReviewerCase | None = None
    fake_scenario: str | None = None  # dry run only: which scripted scenario to use
    cassette: bool = False  # record the first real run for the free CI replay

    def fingerprint(self) -> str:
        """Hash of the scenario as written: the report records it, so any later edit shows."""
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


def load(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    out: list[Scenario] = []
    for f in sorted(directory.glob("*.yaml")):
        for raw in yaml.safe_load(f.read_text()) or []:
            out.append(Scenario.model_validate(raw))
    ids = [s.id for s in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    for s in out:
        if s.kind == "reviewer" and s.reviewer_case is None:
            raise ValueError(f"{s.id}: reviewer scenario without reviewer_case")
        if s.kind != "reviewer" and not s.request:
            raise ValueError(f"{s.id}: request required")
        if s.kind == "memory_ab" and not s.prime:
            raise ValueError(f"{s.id}: memory_ab needs prime tasks")
    return out


def decision_for(rules: list[Rule], kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    for r in rules:
        if r.kind != kind:
            continue
        if r.tool and payload.get("tool") != r.tool:
            continue
        if r.to_domain:
            to = str((payload.get("args") or {}).get("to", ""))
            if not to.lower().endswith("@" + r.to_domain.lower()):
                continue
        return r.decision
    return DEFAULT_DECISIONS.get(kind, {"action": "reject"})
