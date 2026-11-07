"""ProposedAction hashing and the dedupe reducer (Phase 3 change 2; Phase 4 depends on it)."""

import pytest

from agentops.core.state import ProposedAction, merge_proposals
from agentops.tools.registry import action_hash

pytestmark = pytest.mark.unit


def pa(h: str, revision: int = 0, rationale: str | None = None) -> ProposedAction:
    return ProposedAction(
        hash=h,
        tool="send_email",
        args={"to": "a@b.co"},
        risk="external",
        agent="writing",
        subtask_id="s",
        revision=revision,
        rationale=rationale,
    )


def test_hash_is_canonical_and_ignores_who_and_when() -> None:
    a = action_hash("t1", "send_email", {"to": "a@b.co", "subject": "x"})
    b = action_hash("t1", "send_email", {"subject": "x", "to": "a@b.co"})
    assert a == b
    assert a != action_hash("t2", "send_email", {"to": "a@b.co", "subject": "x"})
    assert a != action_hash("t1", "send_email", {"to": "a@b.co", "subject": "y"})


def test_reducer_keeps_exactly_one_per_hash_first_wins() -> None:
    first = {"h1": pa("h1", revision=0, rationale="original")}
    rerun = {"h1": pa("h1", revision=0, rationale="re-run after crash")}
    later = {"h1": pa("h1", revision=2), "h2": pa("h2")}
    merged = merge_proposals(merge_proposals(first, rerun), later)
    assert sorted(merged) == ["h1", "h2"]
    assert merged["h1"].rationale == "original"
    assert merge_proposals(None, None) == {}
