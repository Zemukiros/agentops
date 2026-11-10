"""Versioned model price table (ADR-007, ADR-009, ADR-024).

Rules:
- Never hardcode a real provider price from memory. A real-model entry is `verified=True` only
  with a `source` naming the official pricing page and the date it was checked.
- An unknown/unverified price yields `cost_usd=None` (recorded as NULL + counted as an
  unpriced call), never a guessed number. In LLM_MODE=real the ModelClient refuses to call
  an unpriced model at all, because the budget reservation needs a worst-case cost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PRICE_TABLE_VERSION = "2026-11-10.1"


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input_per_mtok: float | None  # USD per 1M input tokens
    output_per_mtok: float | None  # USD per 1M output tokens
    verified: bool
    source: str  # where/when the price was checked, or why it is unverified
    # Optional long-prompt tier: when a request's input tokens exceed the threshold, the
    # whole request is billed at the long-tier rates.
    long_input_threshold: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None

    @property
    def priced(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None


PRICES: dict[tuple[str, str], ModelPrice] = {
    ("fake", "fake-scripted-v1"): ModelPrice(
        "fake", "fake-scripted-v1", 0.0, 0.0, verified=True, source="local fake model, free"
    ),
    # Fake model with non-zero prices, used only by tests that exercise budget math.
    # Nothing is billed: it is the same scripted model.
    ("fake", "fake-priced-v1"): ModelPrice(
        "fake", "fake-priced-v1", 1.0, 5.0, verified=True, source="test fixture, not billed"
    ),
    # Real models. Checked against https://platform.claude.com/docs/en/about-claude/pricing
    # on 2026-11-05 (first-party Claude API, standard tier, no caching, no inference_geo).
    ("anthropic", "claude-sonnet-5-5"): ModelPrice(
        "anthropic",
        "claude-sonnet-5-5",
        2.0,
        10.0,
        verified=True,
        source="platform.claude.com/docs/en/about-claude/pricing, checked 2026-11-05",
    ),
    # Eval judge (Phase 6). Checked against the same page on 2026-11-10.
    ("anthropic", "claude-opus-5-5"): ModelPrice(
        "anthropic",
        "claude-opus-5-5",
        4.0,
        20.0,
        verified=True,
        source="platform.claude.com/docs/en/about-claude/pricing, checked 2026-11-10",
    ),
    ("anthropic", "claude-haiku-5-5"): ModelPrice(
        "anthropic",
        "claude-haiku-5-5",
        0.10,
        0.50,
        verified=True,
        source="platform.claude.com/docs/en/about-claude/pricing, checked 2026-11-05",
        long_input_threshold=100_000,  # "prompts over 100,000 tokens" pay the long tier
        long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
    ),
}


def lookup(provider: str, model: str) -> ModelPrice | None:
    return PRICES.get((provider, model))


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = lookup(provider, model)
    if price is None or not price.priced:
        log.warning("no verified price for %s/%s; cost recorded as NULL", provider, model)
        return None
    assert price.input_per_mtok is not None and price.output_per_mtok is not None
    in_rate, out_rate = price.input_per_mtok, price.output_per_mtok
    if price.long_input_threshold is not None and input_tokens > price.long_input_threshold:
        if price.long_input_per_mtok is None or price.long_output_per_mtok is None:
            log.warning("%s/%s long-tier price unknown; cost recorded as NULL", provider, model)
            return None
        in_rate, out_rate = price.long_input_per_mtok, price.long_output_per_mtok
    return (input_tokens * in_rate + output_tokens * out_rate) / 1e6
