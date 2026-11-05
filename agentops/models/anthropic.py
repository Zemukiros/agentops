"""Real-model factory for LLM_MODE=real: Anthropic models through langchain-anthropic
(ADR-009). Verified against langchain-anthropic 1.7.10 / anthropic 2.6.0 on 2026-11-05:

- Structured output must use `method="json_schema"` (output_config.format). The default
  `function_calling` relies on forced tool_choice, which Sonnet 5.5 rejects (400).
- `temperature` defaults to None and is only sent when set; we never set it.
- `reasoning_effort` maps to `output_config.effort` and turns on adaptive thinking.
  Thinking tokens bill as output and count toward `max_tokens`, which we always set, so the
  ledger's output worst case still holds.
- `max_retries=0`: ModelClient owns retries so every attempt is recorded and reserved.
- `get_num_tokens_from_messages` calls the free count_tokens API with messages, system and
  tools only. We add a margin for what it can't see (ADR-024).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

from agentops.models import pricing
from agentops.models.client import ChatFactory, ModelConfigError
from agentops.settings import RoleModel, Settings


class MarginChatAnthropic(ChatAnthropic):
    """ChatAnthropic whose input-token count includes a safety margin for budget reservation."""

    input_margin_ratio: float = 1.15
    input_margin_tokens: int = 512

    def get_num_tokens_from_messages(
        self,
        messages: list[BaseMessage],
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool] | None = None,
        **kwargs: Any,
    ) -> int:
        counted = super().get_num_tokens_from_messages(messages, tools=tools, **kwargs)
        return math.ceil(counted * self.input_margin_ratio) + self.input_margin_tokens


def anthropic_factory(settings: Settings) -> ChatFactory:
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise ModelConfigError("LLM_MODE=real with anthropic models needs ANTHROPIC_API_KEY")
    key = settings.anthropic_api_key

    def make(role: str, purpose: str, cfg: RoleModel) -> BaseChatModel:
        if cfg.provider != "anthropic":
            raise ModelConfigError(f"role {role}: provider {cfg.provider!r} not supported")
        kwargs: dict[str, Any] = {}
        if cfg.effort is not None:
            kwargs["reasoning_effort"] = cfg.effort
        return MarginChatAnthropic(
            model=cfg.model,
            max_tokens=cfg.max_output_tokens,
            api_key=key,
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
            input_margin_ratio=settings.llm_input_margin_ratio,
            input_margin_tokens=settings.llm_input_margin_tokens,
            **kwargs,
        )

    return make


def required_roles() -> list[str]:
    """Roles the main graph calls in this phase: supervisor, reviewer, enabled specialists."""
    from agentops.core.scheduler import ENABLED_SPECIALISTS  # noqa: PLC0415

    return ["supervisor", "reviewer", *sorted(ENABLED_SPECIALISTS)]


def real_factory(settings: Settings) -> ChatFactory:
    """Fail fast (at worker start) if a role the graph uses isn't a priced real model.
    Note: ROLE_MODELS__* env vars replace the whole default role map, so a role you don't
    set falls back to the fake provider; this check catches that."""
    bad = {
        r: f"{settings.model_for(r).provider}/{settings.model_for(r).model}"
        for r in required_roles()
        if settings.model_for(r).provider != "anthropic"
    }
    if bad:
        raise ModelConfigError(f"LLM_MODE=real: these roles have no real model: {bad}")
    for r in required_roles():
        cfg = settings.model_for(r)
        price = pricing.lookup(cfg.provider, cfg.model)
        if price is None or not price.priced:
            raise ModelConfigError(f"LLM_MODE=real: no verified price for role {r} ({cfg.model})")
    return anthropic_factory(settings)
