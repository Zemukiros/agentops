"""Plan validation and every scheduler case (ADR-002, ADR-026)."""

import pytest

from agentops.core.scheduler import decide, max_waves, validate_plan
from agentops.core.state import Plan, Subtask, SubtaskResult

pytestmark = pytest.mark.unit


def st(sid: str, *deps: str, specialist: str = "writing") -> Subtask:
    return Subtask(
        id=sid,
        description=f"do {sid}",
        specialist=specialist,
        depends_on=list(deps),
        expected_output="text",
    )


def plan(*subtasks: Subtask) -> Plan:
    return Plan(subtasks=list(subtasks), confidence=0.7, rationale="r")


def res(sid: str, status: str) -> SubtaskResult:
    return SubtaskResult(subtask_id=sid, status=status)


# ---- validate_plan --------------------------------------------------------------------------
def test_valid_plan_has_no_errors() -> None:
    assert validate_plan(plan(st("a"), st("b", "a"), st("c", "a", "b"))) == []


@pytest.mark.parametrize(
    ("p", "needle"),
    [
        (plan(st("a", "b"), st("b", "a")), "dependency cycle among ['a', 'b']"),
        (plan(st("a"), st("b", "c"), st("c", "b")), "dependency cycle among ['b', 'c']"),
        (plan(st("a", "zzz")), "unknown ids ['zzz']"),
        (plan(st("a"), st("a")), "duplicate subtask ids: ['a']"),
        (plan(st("a", specialist="research")), "specialist 'research' is not available"),
        (plan(*(st(f"s{i}") for i in range(7))), "too many subtasks: 7 > 6"),
        (plan(st("a", "a")), "depends on itself"),
    ],
)
def test_invalid_plans(p: Plan, needle: str) -> None:
    errors = validate_plan(p)
    assert any(needle in e for e in errors), errors


def test_bad_slug_is_rejected_at_parse_time() -> None:
    with pytest.raises(ValueError):
        st("Bad Id!")


# ---- decide ---------------------------------------------------------------------------------
CHAIN = plan(st("a"), st("b", "a"), st("c", "b"))
FORK = plan(st("a"), st("b"), st("c", "a", "b"))


def test_first_wave_dispatches_roots_only() -> None:
    d = decide(FORK, {}, wave=0)
    assert (d.kind, d.subtask_ids) == ("dispatch", ["a", "b"])


def test_ready_after_dependencies_accepted() -> None:
    d = decide(CHAIN, {"a": res("a", "accepted")}, wave=1)
    assert (d.kind, d.subtask_ids) == ("dispatch", ["b"])


def test_all_accepted() -> None:
    results = {s: res(s, "accepted") for s in "abc"}
    assert decide(CHAIN, results, wave=3).kind == "all_accepted"


def test_needs_human_escalates_when_nothing_else_is_ready() -> None:
    d = decide(CHAIN, {"a": res("a", "needs_human")}, wave=1)
    assert (d.kind, d.subtask_ids) == ("escalate", ["a"])


def test_ready_work_runs_before_escalation() -> None:
    d = decide(FORK, {"a": res("a", "needs_human")}, wave=1)
    assert (d.kind, d.subtask_ids) == ("dispatch", ["b"])
    d = decide(FORK, {"a": res("a", "needs_human"), "b": res("b", "accepted")}, wave=2)
    assert (d.kind, d.subtask_ids) == ("escalate", ["a"])


def test_failed_dependency_fails_with_blocked_dependents() -> None:
    d = decide(CHAIN, {"a": res("a", "failed")}, wave=1)
    assert d.kind == "fail"
    assert d.reason == "subtask(s) ['a'] failed; blocked dependents: ['b', 'c']"


def test_failure_wins_over_ready_and_needs_human() -> None:
    """Once a subtask failed there is no path to all-accepted; stop spending."""
    d = decide(FORK, {"a": res("a", "failed"), "b": res("b", "needs_human")}, wave=1)
    assert d.kind == "fail"


def test_failed_leaf_still_fails() -> None:
    results = {"a": res("a", "accepted"), "b": res("b", "accepted"), "c": res("c", "failed")}
    d = decide(FORK, results, wave=2)
    assert d.kind == "fail" and "blocked" not in (d.reason or "")


def test_wave_cap() -> None:
    assert max_waves(CHAIN) == 3
    d = decide(CHAIN, {"a": res("a", "accepted"), "b": res("b", "accepted")}, wave=3)
    assert d.kind == "fail" and "wave cap" in (d.reason or "")


def test_stall_never_falls_through() -> None:
    """A dependency on an id that has no result and can never run (only possible if an
    invalid plan slipped through) is reported, not silently ignored."""
    broken = plan(st("a", "ghost"))
    d = decide(broken, {}, wave=0)
    assert d.kind == "fail" and "stalled" in (d.reason or "")


@pytest.mark.parametrize("statuses", [("accepted",), ("needs_human",), ("failed",), ()])
def test_every_combination_yields_exactly_one_decision(statuses: tuple[str, ...]) -> None:
    import itertools  # noqa: PLC0415

    for combo in itertools.product([*statuses, None], repeat=3):
        results = {sid: res(sid, s) for sid, s in zip("abc", combo, strict=True) if s}
        for p in (CHAIN, FORK):
            d = decide(p, results, wave=1)
            assert d.kind in {"dispatch", "escalate", "fail", "all_accepted"}
            if d.kind == "fail":
                assert d.reason
