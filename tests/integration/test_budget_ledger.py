"""Budget ledger (ADR-024): worst case reserved before every call, settled after."""

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentops.core.budget import BudgetExceededError, BudgetLedger, usd_round_up
from agentops.models import pricing
from agentops.models.client import ModelClient, fake_factory
from agentops.models.fake import FakeScript, ScriptedTurn
from agentops.settings import Budget, RoleModel, Settings
from tests.integration.conftest import SeededUser, create_task

pytestmark = pytest.mark.integration

PRICED = RoleModel(provider="fake", model="fake-priced-v1", max_output_tokens=2000)
OUT_CAP_USD = 2000 * 5.0 / 1e6  # $0.01: fake-priced-v1 output is $5/MTok


async def _no_sleep(_: float) -> None:
    return None


def _client(sm: async_sessionmaker[AsyncSession], script: FakeScript) -> ModelClient:
    settings = Settings(role_models={"writing": PRICED})
    return ModelClient(settings, sm, fake_factory(script), sleep=_no_sleep)


async def _llm_rows(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        return int((await c.execute(text("SELECT count(*) FROM llm_calls"))).scalar_one())


async def test_single_call_cannot_overshoot(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    """Remaining budget is positive but smaller than one call's worst case: refused up front,
    even though the call would actually have been cheap."""
    tid = await create_task(engine, alice, budget=Budget(max_usd=OUT_CAP_USD * 0.9))
    script = FakeScript().add("writing", "*", ScriptedTurn(content="short"))
    client = _client(sessionmaker, script)
    with pytest.raises(BudgetExceededError, match="cost limit: next call may cost"):
        await client.invoke("writing", [HumanMessage("hi")], task_id=tid)
    assert await _llm_rows(engine) == 0  # never called
    snap = await client.ledger.snapshot(tid)
    assert (snap.usd_used, snap.usd_reserved, snap.llm_calls) == (0.0, 0.0, 0)


async def test_spend_never_exceeds_limit_over_many_calls(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    limit = 0.05
    tid = await create_task(engine, alice, budget=Budget(max_usd=limit, max_llm_calls=1000))
    script = FakeScript().add("writing", "*", ScriptedTurn(content="x" * 400))
    client = _client(sessionmaker, script)
    calls = 0
    with pytest.raises(BudgetExceededError):
        for _ in range(100):
            await client.invoke("writing", [HumanMessage("hello " * 50)], task_id=tid)
            calls += 1
            snap = await client.ledger.snapshot(tid)
            assert snap.usd_used <= limit
    snap = await client.ledger.snapshot(tid)
    assert calls > 0 and snap.llm_calls == calls
    assert snap.usd_used <= limit
    # refused because the next worst case didn't fit, while real spend was still under limit
    assert snap.usd_used + OUT_CAP_USD > limit
    assert snap.usd_reserved == 0.0
    async with engine.connect() as c:
        recorded = (await c.execute(text("SELECT sum(cost_usd) FROM llm_calls"))).scalar_one()
    assert recorded == pytest.approx(snap.usd_used)


async def test_concurrent_reservations_only_one_fits(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice, budget=Budget(max_usd=1.0))
    ledger = BudgetLedger(sessionmaker)
    results = await asyncio.gather(
        *(ledger.reserve(tid, usd=0.6, tokens=10) for _ in range(5)), return_exceptions=True
    )
    granted = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BudgetExceededError)]
    assert len(granted) == 1 and len(refused) == 4
    snap = await ledger.snapshot(tid)
    assert snap.usd_reserved == pytest.approx(0.6) and snap.calls_reserved == 1


async def test_orphaned_reservation_charged_at_worst_case_once(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    tid = await create_task(engine, alice)
    ledger = BudgetLedger(sessionmaker)
    res = await ledger.reserve(tid, usd=0.02, tokens=500)
    # crash: never settled. The next segment charges it at worst case.
    assert await ledger.charge_orphans(tid) == 1
    assert await ledger.charge_orphans(tid) == 0
    snap = await ledger.snapshot(tid)
    assert snap.usd_used == pytest.approx(0.02)
    assert (snap.tokens_used, snap.llm_calls) == (500, 1)
    assert (snap.usd_reserved, snap.tokens_reserved, snap.calls_reserved) == (0.0, 0, 0)
    # a late settle from the zombie attempt must not count the call twice
    await ledger.settle(res, usd=0.001, tokens=40)
    assert await ledger.snapshot(tid) == snap


async def test_settle_records_truth_even_beyond_reservation(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], alice: SeededUser
) -> None:
    """If a provider ever reports more than reserved (an estimation bug), record it anyway."""
    tid = await create_task(engine, alice, budget=Budget(max_usd=0.01))
    ledger = BudgetLedger(sessionmaker)
    res = await ledger.reserve(tid, usd=0.01, tokens=100)
    await ledger.settle(res, usd=0.015, tokens=150)
    snap = await ledger.snapshot(tid)
    assert snap.usd_used == pytest.approx(0.015) and snap.tokens_used == 150
    with pytest.raises(BudgetExceededError):
        await ledger.reserve(tid, usd=0.0, tokens=0)


def test_reservation_is_rounded_up_never_down() -> None:
    assert usd_round_up(1.23e-11) == pytest.approx(1e-10)
    assert usd_round_up(0.5) == 0.5
    assert usd_round_up(3e-7) >= 3e-7
    assert pricing.cost_usd("fake", "fake-priced-v1", 1, 0) == pytest.approx(1e-6)
