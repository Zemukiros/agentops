"""plan_gate's effective confidence (ADR-036)."""

import pytest

from agentops.core.scheduler import plan_confidence, plan_depth
from agentops.core.state import Plan, Subtask

pytestmark = pytest.mark.unit


def st(sid: str, *deps: str, complexity: str = "low") -> Subtask:
    return Subtask(
        id=sid,
        description="d",
        specialist="writing",
        depends_on=list(deps),
        expected_output="e",
        complexity=complexity,
    )


def plan(*s: Subtask, conf: float = 0.9) -> Plan:
    return Plan(subtasks=list(s), confidence=conf, rationale="r")


def test_depth() -> None:
    assert plan_depth(plan(st("a"))) == 1
    assert plan_depth(plan(st("a"), st("b", "a"), st("c", "b"), st("d"))) == 3


def test_simple_plan_for_a_new_user_passes_the_default_threshold() -> None:
    c = plan_confidence(plan(st("a")), memory_similarity=0.0, threshold=0.6)
    assert (c.structural, c.effective, c.needs_approval) == (0.9, 0.9, False)


def test_self_reported_low_confidence_is_respected() -> None:
    c = plan_confidence(plan(st("a"), conf=0.3), memory_similarity=1.0, threshold=0.6)
    assert c.effective == 0.3 and c.needs_approval


def test_structure_caps_overconfident_plans() -> None:
    risky = plan(
        st("a", complexity="high"),
        st("b", "a", complexity="high"),
        st("c", "b", complexity="medium"),
        st("d", "c"),
        conf=0.95,
    )
    c = plan_confidence(risky, memory_similarity=0.0, threshold=0.6)
    # 1 - 0.30 (2 high) - 0.05 (1 medium) - 0.20 (depth 4) - 0.10 (unfamiliar) = 0.35
    assert (c.high, c.medium, c.depth, c.structural, c.effective) == (2, 1, 4, 0.35, 0.35)
    assert c.needs_approval


def test_familiarity_from_memory_raises_structural_score() -> None:
    p = plan(st("a", complexity="medium"))
    assert plan_confidence(p, memory_similarity=0.0, threshold=0.6).structural == 0.85
    assert plan_confidence(p, memory_similarity=0.8, threshold=0.6).structural == 0.93
