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
