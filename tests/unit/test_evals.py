"""Eval harness pure parts: statistics, scenario loading, approval rules, judge parsing,
cassette conversion. (The harness end to end is exercised by `python -m evals run --mode dry`.)"""

import pytest

from agentops.settings import Settings
from evals import cassette, estimate, metrics
from evals.judge import parse
from evals.runner import REAL_ROLES, eval_settings, role_env
from evals.spec import DEFAULT_DECISIONS, Rule, decision_for, load

pytestmark = pytest.mark.unit


def test_wilson_matches_known_values() -> None:
    lo, hi = metrics.wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.490, 0.943)
    assert metrics.wilson(0, 10)[0] == pytest.approx(0.0, abs=1e-12)
    assert metrics.wilson(10, 10)[1] == pytest.approx(1.0, abs=1e-12)
    assert metrics.wilson(0, 0) == (0.0, 1.0)


def test_percentile_interpolates() -> None:
    assert metrics.percentile([1, 2, 3, 4], 50) == 2.5
    assert metrics.percentile([5], 95) == 5
    assert metrics.percentile([], 50) is None
    assert metrics.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_precision_recall() -> None:
    pr = metrics.precision_recall([(True, True), (True, False), (False, True), (False, False)])
    assert pr == {"tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5}
    assert metrics.precision_recall([(False, False)])["precision"] is None


def test_paired_lift_is_seeded_and_bounded() -> None:
    pairs = [(1.0, 0.0)] * 6 + [(0.0, 0.0)] * 6
    a, b = metrics.paired_lift(pairs), metrics.paired_lift(pairs)
    assert a == b and a["lift"] == 0.5
    lo, hi = a["low"], a["high"]
    assert lo is not None and hi is not None and lo <= 0.5 <= hi
    assert metrics.paired_lift([])["lift"] is None


def test_scenario_set_is_complete_and_valid() -> None:
    s = load()
    assert len(s) == 60
    cats = {x.category for x in s}
    assert cats == {
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
    }
    assert all(x.fingerprint() == x.fingerprint() for x in s)
    reviewer = [x for x in s if x.kind == "reviewer"]
    assert {x.reviewer_case.defect is None for x in reviewer if x.reviewer_case} == {True, False}


def test_approval_rules_and_careful_defaults() -> None:
    rules = [
        Rule(
            kind="approve_action",
            tool="send_email",
            to_domain="example.com",
            decision={"action": "approve"},
        )
    ]
    ok = {"tool": "send_email", "args": {"to": "team@example.com"}}
    evil = {"tool": "send_email", "args": {"to": "x@evil.example"}}
    assert decision_for(rules, "approve_action", ok) == {"action": "approve"}
    assert decision_for(rules, "approve_action", evil) == DEFAULT_DECISIONS["approve_action"]
    assert DEFAULT_DECISIONS["approve_action"]["action"] == "reject"  # actions need a rule
    assert decision_for([], "budget", {})["action"] == "reject"


def test_judge_parse_fails_missing_criteria() -> None:
    rubric = ["a", "b", "c"]
    crit, passed = parse(
        {
            "criteria": [
                {"index": 0, "pass": True, "reason": "ok"},
                {"index": 2, "pass": True, "reason": "ok"},
            ]
        },
        rubric,
    )
    assert not passed and crit[1] == {"criterion": "b", "pass": False, "reason": "not graded"}
    _, passed = parse(
        {"criteria": [{"index": i, "pass": True, "reason": "r"} for i in range(3)]}, rubric
    )
    assert passed


def test_cassette_turns_and_script() -> None:
    plan = cassette.to_turn(
        "plan/p1", {"content": '{"subtasks": [], "confidence": 0.9, "rationale": "r"}'}
    )
    assert plan.structured == {"subtasks": [], "confidence": 0.9, "rationale": "r"}
    act = cassette.to_turn(
        "act/r0/x",
        {
            "content": "",
            "tool_calls": [
                {"name": "web_search", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}
            ],
        },
    )
    assert act.tool_calls == [{"name": "web_search", "args": {"query": "q"}}]
    script = cassette.script_for(
        {
            "scenario": "s1",
            "calls": [
                {"role": "writing", "purpose": "act/r0/x", "response": {"content": "one"}},
                {"role": "writing", "purpose": "act/r0/x", "response": {"content": "two"}},
            ],
        }
    )
    turn = script.get("writing", "cassette:s1", 1, "act/r0/x")
    assert turn is not None and turn.content == "two"


def test_estimate_scales_with_k_and_kind() -> None:
    s = {x.id: x for x in load()}
    assert estimate.agent_cost(s["rev-clean-summary"]) == estimate.REVIEWER_CALL
    one = estimate.agent_cost(s["plan-simple-summary"])
    assert estimate.agent_cost(s["mem-sources-line"]) == pytest.approx(one * 3)


def test_real_mode_uses_real_role_models_in_process_and_in_worker_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: real mode once kept the fake role models from .env, so every paid run
    would have failed with ModelConfigError (caught at $0 when the baseline started)."""
    s = eval_settings(Settings(), "real")
    assert {r: m.provider for r, m in s.role_models.items()} == dict.fromkeys(
        REAL_ROLES, "anthropic"
    )
    for k, v in role_env(s).items():
        monkeypatch.setenv(k, v)
    sub = Settings()  # what a worker subprocess builds from its environment
    assert sub.role_models == s.role_models
    assert eval_settings(Settings(), "dry").role_models == Settings().role_models
