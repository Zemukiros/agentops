"""Scripted fake-mode scenarios for the `main` graph (LLM_MODE=fake, ADR-009/027).

Each scenario scripts the supervisor (purposes `plan`, `synthesize`), the writing specialist
(`draft`) and the reviewer (`review`). Turn index = number of AI messages in the prompt:
plan attempt - 1 for the planner, revision for the writer and reviewer (see core/prompts.py).

A task picks a scenario with POST /tasks {"scenario": "..."} (fake mode only); default is
`default`. These scripts are test fixtures for the orchestration logic. They say nothing
about how well real models plan or write.
"""

from __future__ import annotations

from typing import Any

from agentops.models.fake import FakeScript, ScriptedTurn

DEFAULT_SCENARIO = "default"


def _subtask(
    sid: str, deps: list[str] | None = None, specialist: str = "writing"
) -> dict[str, Any]:
    return {
        "id": sid,
        "description": f"Write the {sid} section for the request.",
        "specialist": specialist,
        "depends_on": deps or [],
        "expected_output": f"A short markdown {sid}.",
        "complexity": "low",
    }


def _plan(*subtasks: dict[str, Any], confidence: float = 0.8) -> ScriptedTurn:
    return ScriptedTurn(
        structured={
            "subtasks": list(subtasks),
            "confidence": confidence,
            "rationale": "scripted plan",
        }
    )


def _verdict(verdict: str, score: int, feedback: str | None = None) -> ScriptedTurn:
    return ScriptedTurn(
        structured={
            "verdict": verdict,
            "score": score,
            "issues": [] if verdict == "accept" else ["scripted issue"],
            "feedback_for_specialist": feedback,
            "unsupported_claims": [],
        }
    )


ONE_SUBTASK = _plan(_subtask("draft"))
ACCEPT = _verdict("accept", 5)
REVISE = _verdict("revise", 2, "Add a concrete example.")
DRAFTS = (
    ScriptedTurn(content="Draft v1: the summary."),
    ScriptedTurn(content="Draft v2: the summary, with a concrete example."),
    ScriptedTurn(content="Draft v3: the summary, with two concrete examples."),
)
FINAL = ScriptedTurn(content="Final deliverable: the summary, with a concrete example.")


def build_scenarios() -> FakeScript:
    s = FakeScript()
    # Defaults shared by every scenario (wildcard scenario "*"); scenarios override below.
    s.add("supervisor", "*", ONE_SUBTASK, purpose="plan")
    s.add("supervisor", "*", FINAL, purpose="synthesize")
    s.add("writing", "*", *DRAFTS, purpose="draft")
    s.add("reviewer", "*", ACCEPT, purpose="review")

    # revise once, then accept
    s.add("reviewer", "revise_once", REVISE, ACCEPT, purpose="review")
    # reviewer never satisfied: 2 revisions, then needs_human (last turn repeats)
    s.add("reviewer", "revisions_exhausted", REVISE, purpose="review")
    # reviewer hands it to a human straight away
    s.add("reviewer", "reviewer_escalates", _verdict("escalate", 2, None), purpose="review")
    # two subtasks, the second depends on the first
    s.add(
        "supervisor",
        "chain",
        _plan(_subtask("outline"), _subtask("draft", ["outline"])),
        purpose="plan",
    )
    # first plan has a cycle; the re-plan is valid
    cyclic = _plan(_subtask("a", ["b"]), _subtask("b", ["a"]))
    s.add("supervisor", "bad_plan_then_fixed", cyclic, ONE_SUBTASK, purpose="plan")
    # both plans invalid: a cycle, then a specialist that isn't enabled in Phase 2
    s.add(
        "supervisor",
        "bad_plan_twice",
        cyclic,
        _plan(_subtask("x", specialist="research")),
        purpose="plan",
    )
    # the writing model fails on every attempt -> subtask failed -> run failed
    s.add(
        "writing",
        "specialist_error",
        ScriptedTurn(error="provider 529 overloaded"),
        purpose="draft",
    )
    return s


SCENARIOS = build_scenarios()
SCENARIO_NAMES = frozenset(
    {
        DEFAULT_SCENARIO,
        "revise_once",
        "revisions_exhausted",
        "reviewer_escalates",
        "chain",
        "bad_plan_then_fixed",
        "bad_plan_twice",
        "specialist_error",
    }
)
