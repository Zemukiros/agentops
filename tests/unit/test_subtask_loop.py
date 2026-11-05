"""The revise loop terminates at the cap whatever the reviewer says (DECISIONS invariant)."""

import itertools

import pytest

from agentops.core.nodes.subtask import (
    MAX_REVISIONS,
    after_review,
    after_specialist,
    terminal_result,
)
from agentops.core.state import Review, ReviewVerdict, Subtask, SubtaskState

pytestmark = pytest.mark.unit

SUB = Subtask(id="s", description="d", specialist="writing", expected_output="e")


def simulate(verdicts: list[str]) -> tuple[SubtaskState, int, int]:
    """Drive the subgraph's routing functions the way LangGraph would, with the review node's
    state update (revision bump only while a revise is still allowed)."""
    state: SubtaskState = {
        "task_id": "t",
        "request": "r",
        "subtask": SUB,
        "context": {},
        "wave": 1,
        "revision": 0,
        "drafts": [],
        "local_reviews": [],
    }
    specialist_calls = review_calls = 0
    it = iter(verdicts)
    node = "specialist"
    for _ in range(100):  # hard stop for the test itself
        if node == "specialist":
            specialist_calls += 1
            state["drafts"] = [*state["drafts"], f"d{specialist_calls}"]
            node = after_specialist(state)
        elif node == "review":
            review_calls += 1
            v = next(it, verdicts[-1])
            rev = state["revision"]
            state["local_reviews"] = [
                *state["local_reviews"],
                Review(subtask_id="s", revision=rev, verdict=ReviewVerdict(verdict=v, score=3)),
            ]
            if v == "revise" and rev < MAX_REVISIONS:
                state["revision"] = rev + 1
            node = after_review(state)
        else:
            return state, specialist_calls, review_calls
    raise AssertionError("loop did not terminate")


def test_always_revise_hits_the_cap_then_needs_human() -> None:
    state, spec, rev = simulate(["revise"])
    assert (spec, rev) == (MAX_REVISIONS + 1, MAX_REVISIONS + 1) == (3, 3)
    r = terminal_result(state)
    assert r.status == "needs_human" and r.revisions == 2 and r.output == "d3"
    assert r.reason == "revisions exhausted (2)"


def test_revise_once_then_accept() -> None:
    state, spec, rev = simulate(["revise", "accept"])
    r = terminal_result(state)
    assert (spec, rev, r.status, r.revisions, r.output) == (2, 2, "accepted", 1, "d2")


def test_escalate_stops_immediately() -> None:
    state, spec, rev = simulate(["escalate"])
    r = terminal_result(state)
    assert (spec, rev, r.status, r.reason) == (1, 1, "needs_human", "reviewer escalated")


@pytest.mark.parametrize(
    "verdicts", list(itertools.product(["accept", "revise", "escalate"], repeat=4))
)
def test_any_verdict_sequence_terminates_within_cap(verdicts: tuple[str, ...]) -> None:
    state, spec, rev = simulate(list(verdicts))
    assert spec <= MAX_REVISIONS + 1 and rev <= MAX_REVISIONS + 1
    assert terminal_result(state).status in {"accepted", "needs_human"}


def test_specialist_failure_short_circuits() -> None:
    state: SubtaskState = {
        "task_id": "t",
        "request": "r",
        "subtask": SUB,
        "context": {},
        "wave": 1,
        "failed_reason": "specialist failed: boom",
    }
    assert after_specialist(state) == "finish"
    r = terminal_result(state)
    assert r.status == "failed" and r.reason == "specialist failed: boom"
