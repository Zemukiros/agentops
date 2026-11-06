"""The attempt loop terminates at the cap whatever the reviewer or the specialist does
(DECISIONS invariant; Phase 3 change 5: a step cap is a failed attempt, never an exception)."""

import itertools

import pytest
from langchain_core.messages import AIMessage

from agentops.core.nodes.subtask import (
    MAX_REVISIONS,
    after_close,
    after_review,
    after_specialist,
    after_tools,
    record_attempt,
    terminal_result,
)
from agentops.core.state import Review, ReviewVerdict, Subtask, SubtaskState

pytestmark = pytest.mark.unit

SUB = Subtask(id="s", description="d", specialist="research", expected_output="e")
CAP = 3


def base() -> SubtaskState:
    return {"task_id": "t", "request": "r", "subtask": SUB, "context": {}, "wave": 1}


def simulate(outcomes: list[str]) -> tuple[SubtaskState, int]:
    """Each outcome is how one attempt ends: "step_cap", or a reviewer verdict. Drives the
    routing functions and the pure record step the way the graph does."""
    state = base()
    it = iter(outcomes)
    for attempts in range(1, 51):
        outcome = next(it, outcomes[-1])
        if outcome == "step_cap":
            state["messages"] = [AIMessage("", tool_calls=[{"name": "x", "args": {}, "id": "1"}])]
            state["attempt_outcome"] = "step_cap"
            assert after_tools(state) == "close_attempt"
        else:
            state["messages"] = [AIMessage(f"draft {attempts}")]
            state["draft"] = f"draft {attempts}"
            state["attempt_outcome"] = "reviewed"
            assert after_specialist(state) == "review"
            rev = state.get("revision", 0)
            state["local_reviews"] = [
                *state.get("local_reviews", []),
                Review(
                    subtask_id="s",
                    revision=rev,
                    verdict=ReviewVerdict(verdict=outcome, score=3),
                ),
            ]
            assert after_review(state) == "close_attempt"
        state.update(record_attempt(state, CAP))  # type: ignore[typeddict-item]
        if after_close(state) == "finish":
            return state, attempts
    raise AssertionError("loop did not terminate")


def test_always_revise_hits_the_cap_then_needs_human() -> None:
    state, attempts = simulate(["revise"])
    assert attempts == MAX_REVISIONS + 1 == 3
    r = terminal_result(state)
    assert (r.status, r.revisions, r.output) == ("needs_human", 2, "draft 3")
    assert r.reason == "revisions exhausted (2)"


def test_revise_once_then_accept() -> None:
    state, attempts = simulate(["revise", "accept"])
    r = terminal_result(state)
    assert (attempts, r.status, r.revisions, r.output) == (2, "accepted", 1, "draft 2")


def test_escalate_stops_immediately() -> None:
    state, attempts = simulate(["escalate"])
    assert (attempts, terminal_result(state).reason) == (1, "reviewer escalated")


def test_step_cap_counts_as_a_failed_attempt_then_recovers() -> None:
    state, attempts = simulate(["step_cap", "accept"])
    r = terminal_result(state)
    assert (attempts, r.status, r.revisions) == (2, "accepted", 1)
    assert [h.outcome for h in state["history"]] == ["step_cap", "reviewed"]
    assert "used all 3 tool calls" in (state["history"][0].note or "")


def test_step_cap_on_every_attempt_needs_a_human_without_raising() -> None:
    state, attempts = simulate(["step_cap"])
    r = terminal_result(state)
    assert attempts == 3 and r.status == "needs_human" and r.output is None
    assert "step cap reached on the final attempt" in (r.reason or "")


@pytest.mark.parametrize(
    "outcomes",
    list(itertools.product(["accept", "revise", "escalate", "step_cap"], repeat=4)),
)
def test_any_sequence_terminates_within_cap(outcomes: tuple[str, ...]) -> None:
    state, attempts = simulate(list(outcomes))
    assert attempts <= MAX_REVISIONS + 1
    assert terminal_result(state).status in {"accepted", "needs_human"}


def test_model_failure_short_circuits() -> None:
    state = base()
    state["failed_reason"] = "specialist failed: boom"
    assert after_specialist(state) == "finish"
    r = terminal_result(state)
    assert r.status == "failed" and r.reason == "specialist failed: boom"
