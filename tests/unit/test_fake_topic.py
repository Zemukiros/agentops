"""Fake-mode text that names the request's topic (Phase 6, demo realism)."""

import pytest

from agentops.models.fake import ScriptedTurn, render_topic, topic_of

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("request_", "topic"),
    [
        ("Write a short summary of what pgvector is.", "Write a short summary of what pgvector is"),
        ("Compare A and B. Then email the team.", "Compare A and B"),
        ("  spaced\n  out   request ", "spaced out request"),
        ("", "the request"),
        ("word " * 40, ("word " * 16).strip() + "…"),
    ],
)
def test_topic_is_the_first_sentence_cut_at_a_word(request_: str, topic: str) -> None:
    assert topic_of(request_) == topic


def test_render_fills_text_and_nested_args_and_leaves_other_turns_alone() -> None:
    turn = ScriptedTurn(
        content="# {topic}",
        tool_calls=[{"name": "send_email", "args": {"subject": "Re: {topic}", "to": "a@b.co"}}],
    )
    out = render_topic(turn, "pgvector")
    assert out.content == "# pgvector"
    assert out.tool_calls[0]["args"] == {"subject": "Re: pgvector", "to": "a@b.co"}
    plain = ScriptedTurn(content="no placeholder")
    assert render_topic(plain, "x") is plain
