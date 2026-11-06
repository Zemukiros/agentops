"""Prompt builders: attempt history, tool log for the reviewer, untrusted framing notes."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentops.core import prompts
from agentops.core.state import AttemptRecord, ReviewVerdict, Subtask, ToolLogEntry
from agentops.models.fake import turns_since_last_human

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
