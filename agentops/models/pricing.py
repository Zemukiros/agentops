"""Versioned model price table (ADR-007, ADR-009).

Rules:
- Never hardcode a real provider price from memory. Real-model entries stay `verified=False`
  with `None` prices until someone checks the provider's pricing page and records the date.
- An unverified/unknown price yields `cost_usd=None` (recorded as NULL + counted as an
  unpriced call), never a guessed number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PRICE_TABLE_VERSION = "2026-11-05.1"


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input_per_mtok: float | None  # USD per 1M input tokens
    output_per_mtok: float | None  # USD per 1M output tokens
    verified: bool
    source: str  # where/when the price was checked, or why it is unverified


PRICES: dict[tuple[str, str], ModelPrice] = {
    ("fake", "fake-scripted-v1"): ModelPrice(
        "fake", "fake-scripted-v1", 0.0, 0.0, verified=True, source="local fake model, free"
    ),
    # Real models: TO VERIFY before LLM_MODE=real is used (Phase 2). Prices intentionally None.
    ("anthropic", "claude-sonnet-5-5"): ModelPrice(
        "anthropic", "claude-sonnet-5-5", None, None, verified=False, source="to verify"
    ),
    ("anthropic", "claude-haiku-5-5"): ModelPrice(
        "anthropic", "claude-haiku-5-5", None, None, verified=False, source="to verify"
    ),
}


def lookup(provider: str, model: str) -> ModelPrice | None:
    return PRICES.get((provider, model))


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = lookup(provider, model)
    if price is None or price.input_per_mtok is None or price.output_per_mtok is None:
        log.warning("no verified price for %s/%s; cost recorded as NULL", provider, model)
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1e6
