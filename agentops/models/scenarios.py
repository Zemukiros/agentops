"""Scripted fake-mode scenarios for the `main` graph (LLM_MODE=fake, ADR-009/027).

Purposes are qualified (ADR-027): "plan/p2", "act/r1/<subtask>", "review/r0/<subtask>",
"synthesize", "final_review". A script registered for a more specific purpose wins, so a
scenario can target one attempt, one revision or one subtask. Within an attempt, the turn
index counts the model's own answers since the last human message.

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
# intro is independent; facts fails; summary depends on facts -> blocked
THREE_WITH_DEPENDENT = _plan(_subtask("intro"), _subtask("facts"), _subtask("summary", ["facts"]))
ACCEPT = _verdict("accept", 5)
REVISE = _verdict("revise", 2, "Add a concrete example.")
FINAL = ScriptedTurn(content="Final deliverable: the summary, with a concrete example.")
PROVIDER_DOWN = ScriptedTurn(error="provider 529 overloaded")


def _draft(n: int) -> ScriptedTurn:
    return ScriptedTurn(
        content=[
            "Draft v1: the summary.",
            "Draft v2: the summary, with a concrete example.",
            "Draft v3: the summary, with two concrete examples.",
        ][n]
    )


def build_scenarios() -> FakeScript:
    s = FakeScript()
    # Defaults shared by every scenario (wildcard scenario "*"); scenarios override below.
    s.add("supervisor", "*", ONE_SUBTASK, purpose="plan")
    s.add("supervisor", "*", FINAL, purpose="synthesize")
    for r in range(3):
        s.add("writing", "*", _draft(r), purpose=f"act/r{r}")
    s.add("reviewer", "*", ACCEPT, purpose="review")
    s.add("reviewer", "*", _verdict("accept", 4), purpose="final_review")

    # revise once, then accept
    s.add("reviewer", "revise_once", REVISE, purpose="review/r0")
    # reviewer never satisfied: 2 revisions, then needs_human
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
    s.add("supervisor", "bad_plan_then_fixed", cyclic, purpose="plan/p1")
    # both plans invalid: a cycle, then an unknown dependency
    s.add("supervisor", "bad_plan_twice", cyclic, purpose="plan/p1")
    s.add("supervisor", "bad_plan_twice", _plan(_subtask("x", ["ghost"])), purpose="plan/p2")
    # the writing model fails on every attempt -> subtask failed -> nothing usable -> failed
    s.add("writing", "specialist_error", PROVIDER_DOWN, purpose="act")

    # ADR-029 partial results: `facts` fails, `summary` is blocked, `intro` is accepted
    for name in ("partial_ok", "partial_low_score", "partial_with_escalation"):
        s.add("supervisor", name, THREE_WITH_DEPENDENT, purpose="plan")
        s.add("writing", name, PROVIDER_DOWN, purpose="act/facts")
    s.add("reviewer", "partial_low_score", _verdict("escalate", 2), purpose="final_review")
    # intro also needs a human; after approval the run can still end completed_partial
    s.add("reviewer", "partial_with_escalation", REVISE, purpose="review/intro")
    # self-reported confidence below PLAN_CONFIDENCE_THRESHOLD -> plan_gate asks a human
    low = _plan(_subtask("draft"), confidence=0.3)
    s.add("supervisor", "low_confidence_plan", low, purpose="plan")
    # writing proposes an email (external -> never sent; recorded as a ProposedAction)
    s.add(
        "writing",
        "proposes_email",
        ScriptedTurn(
            tool_calls=[
                {
                    "name": "send_email",
                    "args": {
                        "to": "team@example.com",
                        "subject": "Summary ready",
                        "body": "The summary is attached.",
                    },
                }
            ]
        ),
        ScriptedTurn(content="Draft: the summary (an email to the team is proposed)."),
        purpose="act",
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
        "partial_ok",
        "partial_low_score",
        "partial_with_escalation",
        "proposes_email",
        "low_confidence_plan",
    }
)
