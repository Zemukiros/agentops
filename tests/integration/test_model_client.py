from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentops.core.budget import BudgetExceededError
from agentops.models.client import ModelCallError, ModelClient, ModelConfigError, fake_factory
from agentops.models.fake import FakeScript, ScriptedTurn
from agentops.settings import Budget, LLMMode, RoleModel, Settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration


class Review(BaseModel):
    verdict: str
    score: int


async def _no_sleep(_: float) -> None:
    return None


async def _rows(engine: AsyncEngine, table: str = "llm_calls") -> list[dict]:
    async with engine.connect() as c:
        r = await c.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
        return [dict(x._mapping) for x in r]


def _client(sm: async_sessionmaker[AsyncSession], factory: Any, **settings: Any) -> ModelClient:
    return ModelClient(Settings(**settings), sm, factory, sleep=_no_sleep)


async def test_records_tokens_cost_latency_and_settles_budget(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    script = FakeScript().add("research", "*", ScriptedTurn(content="hello world"))
    client = _client(sessionmaker, fake_factory(script))
    res = await client.invoke("research", [HumanMessage("hi")], task_id=tid, purpose="draft")
    assert res.message.content == "hello world" and res.attempts == 1
    rows = await _rows(engine)
    assert len(rows) == 1
    r = rows[0]
    assert r["ok"] and r["role"] == "research" and r["provider"] == "fake"
    assert r["purpose"] == "draft"
    assert r["input_tokens"] > 0 and r["output_tokens"] > 0
    assert r["cost_usd"] == 0.0 and r["latency_ms"] >= 0 and r["price_table_version"]
    snap = await client.ledger.snapshot(tid)
    assert snap.llm_calls == 1 and snap.calls_reserved == 0 and snap.tokens_reserved == 0
    assert snap.tokens_used == r["input_tokens"] + r["output_tokens"] == res.tokens
    [res_row] = await _rows(engine, "budget_reservations")
    assert res_row["status"] == "settled"
    # the reservation was the worst case: exact input + the output cap
    assert (
        res_row["tokens"] == r["input_tokens"] + Settings().model_for("research").max_output_tokens
    )
    events = await _rows(engine, "events")
    assert [e["type"] for e in events] == ["llm.call"]
    assert events[0]["payload"]["llm_call_id"] == r["id"]


class Flaky(BaseChatModel):
    """Fails the first `fail_times` calls (shared counter), then answers."""

    fail_times: int
    counter: Any  # shared mutable list; `Any` stops pydantic from copying it

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def get_num_tokens_from_messages(self, messages: Any, tools: Any = None) -> int:
        return 3

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


def _flaky_factory(
    fail_times: int, counter: list[int] | None = None
) -> Callable[..., BaseChatModel]:
    c = counter if counter is not None else [0]
    return lambda role, purpose, cfg: Flaky(fail_times=fail_times, counter=c)


async def test_every_retry_attempt_is_recorded(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    client = _client(sessionmaker, _flaky_factory(2), llm_max_retries=2)
    res = await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    assert res.attempts == 3
    rows = await _rows(engine)
    assert [(r["attempt"], r["ok"]) for r in rows] == [(1, False), (2, False), (3, True)]
    assert "ConnectionError" in rows[0]["error"]
    snap = await client.ledger.snapshot(tid)
    assert snap.llm_calls == 3  # failed attempts count toward the call budget
    assert [r["status"] for r in await _rows(engine, "budget_reservations")] == ["settled"] * 3


async def test_all_attempts_fail(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    client = _client(sessionmaker, _flaky_factory(99), llm_max_retries=1)
    with pytest.raises(ModelCallError, match="all 2 attempts failed"):
        await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    assert len(await _rows(engine)) == 2


async def test_call_limit_refuses_before_calling(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice, budget=Budget(max_llm_calls=2))
    counter = [0]
    client = _client(sessionmaker, _flaky_factory(0, counter))
    await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    with pytest.raises(BudgetExceededError, match="llm call limit"):
        await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    assert counter == [2]  # the third call never reached the model
    assert len(await _rows(engine)) == 2  # and nothing was recorded for it


async def test_budget_stops_retries_midway(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice, budget=Budget(max_llm_calls=2))
    client = _client(sessionmaker, _flaky_factory(99), llm_max_retries=5)
    with pytest.raises(BudgetExceededError):
        await client.invoke("analysis", [HumanMessage("x")], task_id=tid)
    assert len(await _rows(engine)) == 2


async def test_structured_output_reask_is_recorded(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    script = FakeScript().add(
        "reviewer",
        "*",
        ScriptedTurn(structured={"verdict": "accept"}),  # missing `score` -> invalid
        ScriptedTurn(structured={"verdict": "accept", "score": 4}),
    )
    client = _client(sessionmaker, fake_factory(script))
    res = await client.invoke("reviewer", [HumanMessage("x")], task_id=tid, schema=Review)
    assert res.parsed == Review(verdict="accept", score=4)
    rows = await _rows(engine)
    assert [(r["attempt"], r["ok"]) for r in rows] == [(1, False), (2, True)]
    assert "structured output invalid" in rows[0]["error"]


async def test_fake_mode_unverified_price_records_null_cost(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    client = _client(
        sessionmaker,
        fake_factory(),
        role_models={"writing": RoleModel(provider="test", model="unpriced-model")},
    )
    res = await client.invoke("writing", [HumanMessage("x")], task_id=tid)
    rows = await _rows(engine)
    assert rows[0]["cost_usd"] is None and rows[0]["model"] == "unpriced-model"
    snap = await client.ledger.snapshot(tid)
    assert snap.unpriced_calls == 1 and snap.usd_used == 0.0 and res.usd is None


async def test_real_mode_refuses_unpriced_model(
    sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser, engine: AsyncEngine
) -> None:
    tid = await create_task(engine, alice)
    counter = [0]
    client = ModelClient(
        Settings(
            llm_mode=LLMMode.REAL,
            role_models={"writing": RoleModel(provider="test", model="unpriced-model")},
        ),
        sessionmaker,
        _flaky_factory(0, counter),
    )
    with pytest.raises(ModelConfigError, match="no verified price"):
        await client.invoke("writing", [HumanMessage("x")], task_id=tid)
    assert counter == [0]
