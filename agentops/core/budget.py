"""Per-task budget: checked before every model call (ADR-009), updated after every attempt."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agentops.settings import Budget


class BudgetExceededError(Exception):
    """Raised before a model call when the task has no budget left."""

    def __init__(self, reason: str, state: BudgetState) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


class BudgetState(BaseModel):
    """Immutable budget snapshot carried in graph state."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    max_usd: float
    max_llm_calls: int
    tokens_used: int = 0
    usd_used: float = 0.0
    llm_calls: int = 0
    unpriced_calls: int = 0  # calls whose cost is unknown (unverified price); see pricing.py

    @classmethod
    def from_limits(cls, limits: Budget) -> BudgetState:
        return cls(
            max_tokens=limits.max_tokens,
            max_usd=limits.max_usd,
            max_llm_calls=limits.max_llm_calls,
        )

    def exceeded_reason(self) -> str | None:
        if self.llm_calls >= self.max_llm_calls:
            return f"llm call limit reached ({self.llm_calls}/{self.max_llm_calls})"
        if self.tokens_used >= self.max_tokens:
            return f"token limit reached ({self.tokens_used}/{self.max_tokens})"
        if self.usd_used >= self.max_usd:
            return f"cost limit reached (${self.usd_used:.4f}/${self.max_usd:.2f})"
        return None

    def check(self) -> None:
        reason = self.exceeded_reason()
        if reason is not None:
            raise BudgetExceededError(reason, self)

    def charge(self, *, tokens: int, usd: float | None) -> BudgetState:
        return self.model_copy(
            update={
                "tokens_used": self.tokens_used + tokens,
                "usd_used": self.usd_used + (usd or 0.0),
                "llm_calls": self.llm_calls + 1,
                "unpriced_calls": self.unpriced_calls + (1 if usd is None else 0),
            }
        )

    @property
    def fraction_used(self) -> float:
        return max(
            self.tokens_used / self.max_tokens,
            self.usd_used / self.max_usd if self.max_usd else 0.0,
            self.llm_calls / self.max_llm_calls,
        )
