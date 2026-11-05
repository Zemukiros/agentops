import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from agentops.models.fake import FakeProviderError, FakeScript, ScriptedChatModel, ScriptedTurn

pytestmark = pytest.mark.unit


class Verdict(BaseModel):
    verdict: str
    score: int


def _script() -> FakeScript:
    return (
        FakeScript()
        .add(
            "research",
            "s1",
            ScriptedTurn(tool_calls=[{"name": "web_search", "args": {"query": "pgvector"}}]),
            ScriptedTurn(content="final answer"),
        )
        .add("reviewer", "s1", ScriptedTurn(structured={"verdict": "accept", "score": 5}))
        .add("writing", "s1", ScriptedTurn(error="provider 529"))
    )


async def test_deterministic_across_instances() -> None:
    msgs = [SystemMessage("sys"), HumanMessage("go")]
    a = await ScriptedChatModel(role="research", scenario="s1", script=_script()).ainvoke(msgs)
    b = await ScriptedChatModel(role="research", scenario="s1", script=_script()).ainvoke(msgs)
    assert a.tool_calls == b.tool_calls
    assert a.usage_metadata == b.usage_metadata


async def test_turn_index_follows_conversation() -> None:
    m = ScriptedChatModel(role="research", scenario="s1", script=_script())

    def search(query: str) -> str:
        """Search the web."""
        return query

    first = await m.bind_tools([search]).ainvoke([HumanMessage("go")])
    assert first.tool_calls[0]["name"] == "web_search"
    assert first.tool_calls[0]["args"] == {"query": "pgvector"}
    assert first.tool_calls[0]["id"]
    second = await m.ainvoke([HumanMessage("go"), first, HumanMessage("result")])
    assert second.content == "final answer"
    assert second.usage_metadata and second.usage_metadata["input_tokens"] > 0


async def test_structured_output_via_with_structured_output() -> None:
    m = ScriptedChatModel(role="reviewer", scenario="s1", script=_script())
    out = await m.with_structured_output(Verdict, include_raw=True).ainvoke([HumanMessage("x")])
    assert isinstance(out, dict)
    assert out["parsed"] == Verdict(verdict="accept", score=5)
    assert isinstance(out["raw"], AIMessage)


async def test_scripted_error_raises() -> None:
    m = ScriptedChatModel(role="writing", scenario="s1", script=_script())
    with pytest.raises(FakeProviderError):
        await m.ainvoke([HumanMessage("x")])


async def test_unscripted_role_echoes() -> None:
    out = await ScriptedChatModel(role="code").ainvoke([HumanMessage("hello")])
    assert "[fake:code] hello" in str(out.content)


async def test_wildcard_scenario() -> None:
    s = FakeScript().add("analysis", "*", ScriptedTurn(content="any"))
    out = await ScriptedChatModel(role="analysis", scenario="zzz", script=s).ainvoke("q")
    assert out.content == "any"


async def test_purpose_keeps_same_role_scripts_apart() -> None:
    """ADR-027: the supervisor plans and synthesizes; both are turn 0."""
    s = (
        FakeScript()
        .add("supervisor", "*", ScriptedTurn(content="PLAN"), purpose="plan")
        .add("supervisor", "*", ScriptedTurn(content="FINAL"), purpose="synthesize")
        .add("supervisor", "special", ScriptedTurn(content="SPECIAL PLAN"), purpose="plan")
    )
    plan = ScriptedChatModel(role="supervisor", purpose="plan", scenario="x", script=s)
    synth = ScriptedChatModel(role="supervisor", purpose="synthesize", scenario="x", script=s)
    special = ScriptedChatModel(role="supervisor", purpose="plan", scenario="special", script=s)
    assert (await plan.ainvoke("q")).content == "PLAN"
    assert (await synth.ainvoke("q")).content == "FINAL"
    assert (await special.ainvoke("q")).content == "SPECIAL PLAN"


async def test_token_count_matches_reported_usage_and_output_is_capped() -> None:
    s = FakeScript().add("writing", "*", ScriptedTurn(content="x" * 4000))
    m = ScriptedChatModel(role="writing", script=s, max_tokens=50)
    msgs = [SystemMessage("sys"), HumanMessage("hello there")]
    out = await m.ainvoke(msgs)
    assert out.usage_metadata is not None
    assert out.usage_metadata["input_tokens"] == m.get_num_tokens_from_messages(msgs)
    assert out.usage_metadata["output_tokens"] == 50
    structured = await m.with_structured_output(Verdict, include_raw=True).ainvoke(msgs)
    assert isinstance(structured, dict)
    raw = structured["raw"]
    assert raw.usage_metadata["input_tokens"] == m.get_num_tokens_from_messages(msgs, [Verdict])
