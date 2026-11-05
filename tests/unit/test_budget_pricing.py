import pytest

from agentops.core.budget import BudgetState
from agentops.models import pricing
from agentops.settings import Budget

pytestmark = pytest.mark.unit


def test_fake_model_is_free_and_verified() -> None:
    assert pricing.cost_usd("fake", "fake-scripted-v1", 1000, 1000) == 0.0
    p = pricing.lookup("fake", "fake-scripted-v1")
    assert p is not None and p.verified


def test_unverified_or_unknown_price_is_none_not_guessed() -> None:
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


def test_refusal_reason_accounts_for_reserved_and_next_call() -> None:
    b = BudgetState.from_limits(Budget(max_tokens=100, max_usd=1.0, max_llm_calls=3))
    assert b.refusal_reason(usd=0.5, tokens=50) is None
    b = b.model_copy(update={"usd_used": 0.4, "usd_reserved": 0.3})
    assert "cost limit" in (b.refusal_reason(usd=0.31, tokens=1) or "")
    assert b.refusal_reason(usd=0.3, tokens=1) is None


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"llm_calls": 2, "calls_reserved": 1}, "llm call"),
        ({"tokens_used": 90, "tokens_reserved": 5}, "token"),
        ({"usd_used": 0.99}, "cost"),
    ],
)
def test_each_limit_trips(kw: dict[str, float], reason: str) -> None:
    b = BudgetState(max_tokens=100, max_usd=1.0, max_llm_calls=3, **kw)
    assert reason in (b.refusal_reason(usd=0.02, tokens=10) or "")


def test_budget_is_immutable() -> None:
    b = BudgetState(max_tokens=1, max_usd=1, max_llm_calls=1)
    with pytest.raises(Exception):  # noqa: B017
        b.tokens_used = 5  # type: ignore[misc]


def test_long_prompt_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        pricing.PRICES,
        ("t", "tiered"),
        pricing.ModelPrice(
            "t",
            "tiered",
            0.10,
            0.50,
            verified=True,
            source="test",
            long_input_threshold=100_000,
            long_input_per_mtok=0.50,
            long_output_per_mtok=2.50,
        ),
    )
    assert pricing.cost_usd("t", "tiered", 100_000, 1_000_000) == pytest.approx(0.01 + 0.5)
    assert pricing.cost_usd("t", "tiered", 100_001, 1_000_000) == pytest.approx(
        100_001 * 0.5 / 1e6 + 2.5
    )


def test_verified_real_prices_have_a_source() -> None:
    for p in pricing.PRICES.values():
        if p.provider != "fake":
            assert p.verified and p.priced and "checked" in p.source
    # Haiku 5.5: 1M in + 1M out under the threshold vs over it
    assert pricing.cost_usd("anthropic", "claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert pricing.cost_usd("anthropic", "claude-haiku-5-5", 100_001, 0) == pytest.approx(
        100_001 * 0.5 / 1e6
    )
    assert pricing.cost_usd("anthropic", "claude-sonnet-5-5", 1000, 1000) == pytest.approx(0.012)
