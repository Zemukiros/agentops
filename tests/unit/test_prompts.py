"""Prompt builders: attempt history, tool log for the reviewer, untrusted framing notes."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentops.core import prompts, scheduler
from agentops.core.state import (
    AttemptRecord,
    MemoryHit,
    Plan,
    ReviewVerdict,
    Subtask,
    ToolLogEntry,
)
from agentops.models.fake import turns_since_last_human
from agentops.settings import Settings
from agentops.tools.catalog import build_registry

pytestmark = pytest.mark.unit

SUB = Subtask(id="s", description="find facts", specialist="research", expected_output="facts")
V = ReviewVerdict(verdict="revise", score=2, feedback_for_specialist="cite sources")


def test_new_attempt_carries_history_and_starts_at_turn_zero() -> None:
    history = [
        AttemptRecord(revision=0, outcome="step_cap", note="used all 8 tool calls"),
        AttemptRecord(revision=1, outcome="reviewed", draft="d1", verdict=V, note="cite sources"),
    ]
    msgs = prompts.specialist_messages(SUB, "req", {}, history=history, max_steps=8)
    assert "at most 8 tool calls" in msgs[0].text and 'trust="untrusted"' in msgs[0].text
    kinds = [type(m).__name__ for m in msgs]
    assert kinds == ["SystemMessage", "HumanMessage", "HumanMessage", "AIMessage", "HumanMessage"]
    assert turns_since_last_human(msgs) == 0


def test_reviewer_gets_the_tool_log_and_its_own_earlier_verdicts() -> None:
    history = [AttemptRecord(revision=0, outcome="reviewed", draft="d1", verdict=V, note="n")]
    log = [ToolLogEntry(step=1, tool="web_search", status="ok", args={"query": "x"}, preview="r")]
    msgs = prompts.reviewer_messages(SUB, "req", history=history, draft="d2", tool_log=log)
    assert isinstance(msgs[-2], AIMessage) and '"verdict":"revise"' in msgs[-2].text
    last = msgs[-1]
    assert isinstance(last, HumanMessage)
    assert "<draft>\nd2\n</draft>" in last.text
    assert "1. web_search [ok]" in last.text and "untrusted" in last.text
    assert turns_since_last_human(msgs) == 0


def test_reviewer_sees_the_dependency_context_the_specialist_had() -> None:
    """Regression (Phase 3 real run): the brief's reviewer escalated because it never saw the
    accepted research/analysis outputs the writer was working from."""
    ctx = {"analysis": "North revenue: 12,345", "research": "pgvector adds HNSW indexes"}
    first = prompts.reviewer_messages(SUB, "req", history=[], draft="d", tool_log=[], context=ctx)
    body = first[-1].text
    assert '<context from="analysis">\nNorth revenue: 12,345\n</context>' in body
    assert '<context from="research">' in body
    assert "already reviewed and accepted" in first[0].text
    # and on later revisions the context is still there (in the first human turn)
    history = [AttemptRecord(revision=0, outcome="reviewed", draft="d1", verdict=V, note="n")]
    later = prompts.reviewer_messages(
        SUB, "req", history=history, draft="d2", tool_log=[], context=ctx
    )
    assert "North revenue: 12,345" in later[1].text


def test_planner_sees_what_each_specialist_can_do_from_the_real_registry() -> None:
    """ADR-047: the baseline planner, shown only names, gave SQL work to `code` (no database).
    The catalog lists each specialist's actual tools, and its summaries agree with them."""
    reg = build_registry(Settings())
    tools = {sp: [t.name for t in reg.tools_for(sp)] for sp in scheduler.ENABLED_SPECIALISTS}
    who = {
        name: {sp for sp, ts in tools.items() if name in ts} for name in ("sql_query", "send_email")
    }
    assert who == {"sql_query": {"analysis"}, "send_email": {"writing"}}  # the summaries say so
    system = prompts.planner_messages(
        "q", specialists=sorted(tools), max_subtasks=6, previous=[], tools=tools
    )[0].content
    assert isinstance(system, str)
    for sp, ts in tools.items():
        assert f"- {sp}: {prompts.ROLE_SUMMARY[sp]}" in system
        assert all(t in system for t in ts)


def test_recalled_memories_reach_the_synthesizer() -> None:
    """ADR-048: a remembered format preference must reach the text the user gets."""
    plan = Plan(
        subtasks=[Subtask(id="a", description="d", specialist="writing", expected_output="e")],
        confidence=0.9,
        rationale="r",
    )
    hit = MemoryHit(
        id="m1",
        kind="episode",
        text="Team format: first line is 'Recommendation: X'",
        score=0.7,
        outcome="completed",
        similarity=0.8,
    )
    human = prompts.synth_messages("req", plan, {"a": "out"}, memories=[hit])[1].content
    assert "Recommendation: X" in str(human) and 'note="past example, may not apply"' in str(human)
    assert "past example" not in str(prompts.synth_messages("req", plan, {"a": "out"})[1].content)
