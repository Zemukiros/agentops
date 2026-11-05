from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentops.core.budget import BudgetExceededError, BudgetState
from agentops.models.client import ModelCallError, ModelClient, fake_factory
from agentops.models.fake import FakeScript, ScriptedTurn
from agentops.settings import Budget, RoleModel, Settings

pytestmark = pytest.mark.integration


class Review(BaseModel):
    verdict: str
    score: int


def _budget(**kw: Any) -> BudgetState:
    return BudgetState.from_limits(Budget(**kw))


async def _no_sleep(_: float) -> None:
    return None


async def _rows(engine: AsyncEngine) -> list[dict]:
    async with engine.connect() as c:
        r = await c.execute(text("SELECT * FROM llm_calls ORDER BY id"))
        return [dict(x._mapping) for x in r]


def _client(sm: async_sessionmaker[AsyncSession], factory: Any, **settings: Any) -> ModelClient:
    return ModelClient(Settings(**settings), sm, factory, sleep=_no_sleep)


async def test_records_tokens_cost_latency_and_charges_budget(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    script = FakeScript().add("research", "*", ScriptedTurn(content="hello world"))
    client = _client(sessionmaker, fake_factory(script))
    res = await client.invoke("research", [HumanMessage("hi")], budget=_budget())
    assert res.message.content == "hello world" and res.attempts == 1
    rows = await _rows(engine)
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] and r["role"] == "research" and r["provider"] == "fake"
    assert r["input_tokens"] > 0 and r["output_tokens"] > 0
    assert r["cost_usd"] == 0.0 and r["latency_ms"] >= 0 and r["price_table_version"]
    assert res.budget.llm_calls == 1
    assert res.budget.tokens_used == r["input_tokens"] + r["output_tokens"]


class Flaky(BaseChatModel):
    """Fails the first `fail_times` calls (shared counter), then answers."""

    fail_times: int
    counter: list[int]

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        self.counter[0] += 1
        if self.counter[0] <= self.fail_times:
            raise ConnectionError("upstream 503")
        msg = AIMessage(
            content="ok", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _flaky_factory(fail_times: int) -> Callable[[str, RoleModel], BaseChatModel]:
    counter = [0]
    return lambda role, cfg: Flaky(fail_times=fail_times, counter=counter)


async def test_every_retry_attempt_is_recorded(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client = _client(sessionmaker, _flaky_factory(2), llm_max_retries=2)
    res = await client.invoke("analysis", [HumanMessage("x")], budget=_budget())
    assert res.attempts == 3
    rows = await _rows(engine)
    assert [(r["attempt"], r["ok"]) for r in rows] == [(1, False), (2, False), (3, True)]
    assert "ConnectionError" in rows[0]["error"]
    assert res.budget.llm_calls == 3  # failed attempts count toward the call budget


async def test_all_attempts_fail(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client = _client(sessionmaker, _flaky_factory(99), llm_max_retries=1)
    with pytest.raises(ModelCallError, match="all 2 attempts failed"):
        await client.invoke("analysis", [HumanMessage("x")], budget=_budget())
    assert len(await _rows(engine)) == 2


async def test_budget_checked_before_call(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    counter = [0]
    client = _client(sessionmaker, lambda role, cfg: Flaky(fail_times=0, counter=counter))
    spent = _budget(max_llm_calls=2).charge(tokens=1, usd=0).charge(tokens=1, usd=0)
    with pytest.raises(BudgetExceededError):
        await client.invoke("analysis", [HumanMessage("x")], budget=spent)
    assert counter == [0]  # the model was never called
    assert await _rows(engine) == []  # and nothing was recorded


async def test_budget_stops_retries_midway(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client = _client(sessionmaker, _flaky_factory(99), llm_max_retries=5)
    with pytest.raises(BudgetExceededError):
        await client.invoke("analysis", [HumanMessage("x")], budget=_budget(max_llm_calls=2))
    assert len(await _rows(engine)) == 2


async def test_structured_output_reask_is_recorded(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    script = FakeScript().add(
        "reviewer",
        "*",
        ScriptedTurn(structured={"verdict": "accept"}),  # missing `score` -> invalid
        ScriptedTurn(structured={"verdict": "accept", "score": 4}),
    )
    client = _client(sessionmaker, fake_factory(script))
    res = await client.invoke("reviewer", [HumanMessage("x")], budget=_budget(), schema=Review)
    assert res.parsed == Review(verdict="accept", score=4)
    rows = await _rows(engine)
    assert [(r["attempt"], r["ok"]) for r in rows] == [(1, False), (2, True)]
    assert "structured output invalid" in rows[0]["error"]


async def test_unverified_price_records_null_cost(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client = _client(
        sessionmaker,
        fake_factory(),
        role_models={"writing": RoleModel(provider="anthropic", model="claude-sonnet-5-5")},
    )
    res = await client.invoke("writing", [HumanMessage("x")], budget=_budget())
    rows = await _rows(engine)
    assert rows[0]["cost_usd"] is None and rows[0]["model"] == "claude-sonnet-5-5"
    assert res.budget.unpriced_calls == 1 and res.budget.usd_used == 0.0
