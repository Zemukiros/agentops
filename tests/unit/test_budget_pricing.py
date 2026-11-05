import pytest

from agentops.core.budget import BudgetExceededError, BudgetState
from agentops.models import pricing
from agentops.settings import Budget

pytestmark = pytest.mark.unit


def test_fake_model_is_free_and_verified() -> None:
    assert pricing.cost_usd("fake", "fake-scripted-v1", 1000, 1000) == 0.0
    p = pricing.lookup("fake", "fake-scripted-v1")
    assert p is not None and p.verified


def test_unverified_or_unknown_price_is_none_not_guessed() -> None:
    assert pricing.cost_usd("anthropic", "claude-sonnet-5-5", 1000, 1000) is None
    assert pricing.cost_usd("nope", "nope", 1, 1) is None
    for p in pricing.PRICES.values():
        if not p.verified:
            assert p.input_per_mtok is None and p.output_per_mtok is None


def test_cost_math(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        pricing.PRICES,
        ("t", "m"),
        pricing.ModelPrice("t", "m", 2.0, 10.0, verified=True, source="test"),
    )
    assert pricing.cost_usd("t", "m", 1_000_000, 100_000) == pytest.approx(3.0)


def test_budget_charge_and_check() -> None:
    b = BudgetState.from_limits(Budget(max_tokens=100, max_usd=1.0, max_llm_calls=3))
    b = b.charge(tokens=40, usd=0.1).charge(tokens=40, usd=None)
    assert (b.tokens_used, b.llm_calls, b.unpriced_calls) == (80, 2, 1)
    assert b.usd_used == pytest.approx(0.1)
    b.check()
    b = b.charge(tokens=30, usd=0.0)
    with pytest.raises(BudgetExceededError, match="limit reached"):
        b.check()


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"llm_calls": 3}, "llm call"),
        ({"tokens_used": 100}, "token"),
        ({"usd_used": 1.0}, "cost"),
    ],
)
def test_each_limit_trips(kw: dict[str, float], reason: str) -> None:
    b = BudgetState(max_tokens=100, max_usd=1.0, max_llm_calls=3, **kw)
    assert reason in (b.exceeded_reason() or "")


def test_budget_is_immutable() -> None:
    b = BudgetState(max_tokens=1, max_usd=1, max_llm_calls=1)
    with pytest.raises(Exception):  # noqa: B017
        b.tokens_used = 5  # type: ignore[misc]
