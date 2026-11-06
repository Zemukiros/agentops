"""Deterministic scripted chat model for LLM_MODE=fake (ADR-009).

Determinism without hidden state: the response is chosen by (role, purpose, scenario, turn),
where `turn` = number of AI messages since the last human message. The same input therefore
always produces the same output, in any process. That property is what lets a resumed run on a
different worker see exactly what the first worker saw.

`purpose` is "base[/qualifier...]", e.g. "plan/p2", "act/r1/research". A script registered
for a more specific purpose wins; lookup falls back through subsets of the qualifiers to the
bare base and then "*" (ADR-027). Each new attempt (re-plan, revision, review) starts with a
human message, so its turn index restarts at 0; ReAct steps inside an attempt are turns 1, 2...
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field


class ScriptedTurn(BaseModel):
    """One scripted response. Exactly one of content / tool_calls / structured is the payload."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)  # {"name", "args"}
    structured: dict[str, Any] | None = None  # answered as a call to the bound schema tool
    error: str | None = None  # raise instead of answering (simulates provider failure)


class FakeScript(BaseModel):
    """turns["role:purpose:scenario"] = ordered list of responses by turn index.

    Lookup: the scenario's scripts first (most specific purpose first), then the shared
    "*" scenario the same way.
    """

    turns: dict[str, list[ScriptedTurn]] = Field(default_factory=dict)

    @staticmethod
    def key(role: str, scenario: str, purpose: str = "*") -> str:
        return f"{role}:{purpose}:{scenario}"

    def add(self, role: str, scenario: str, *turns: ScriptedTurn, purpose: str = "*") -> FakeScript:
        self.turns.setdefault(self.key(role, scenario, purpose), []).extend(turns)
        return self

    def get(self, role: str, scenario: str, turn: int, purpose: str = "*") -> ScriptedTurn | None:
        # The scenario's own scripts win over the shared "*" defaults, at any specificity.
        for sc in (scenario, "*"):
            for p in purpose_candidates(purpose):
                seq = self.turns.get(self.key(role, sc, p))
                if seq:
                    return seq[min(turn, len(seq) - 1)]
        return None


def purpose_candidates(purpose: str) -> list[str]:
    """Most specific first: "act/r1/x" -> act/r1/x, act/r1, act/x, act, *."""
    base, *quals = purpose.split("/")
    out: list[str] = []
    for size in range(len(quals), 0, -1):
        for combo in itertools.combinations(quals, size):
            out.append("/".join([base, *combo]))
    out += [base, "*"]
    return out


class FakeProviderError(RuntimeError):
    """Raised for scripted failures; ModelClient treats it as retryable."""


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token). Fake mode only."""
    return max(1, (len(text) + 3) // 4)


REASK = "reask"


def turns_since_last_human(messages: Sequence[BaseMessage]) -> int:
    """Re-ask messages (name="reask", sent by ModelClient after an invalid structured answer)
    continue the same attempt, so they don't reset the count."""
    n = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and m.name != REASK:
            break
        if isinstance(m, AIMessage):
            n += 1
    return n


def _prompt_text(messages: Sequence[BaseMessage], tools: Sequence[dict[str, Any]]) -> str:
    return "".join(m.text for m in messages) + json.dumps(list(tools), sort_keys=True)


class ScriptedChatModel(BaseChatModel):
    role: str
    scenario: str = "default"
    purpose: str = "*"
    script: FakeScript = Field(default_factory=FakeScript)
    model_name: str = "fake-scripted-v1"
    # Like a provider's max_tokens: reported output usage never exceeds it.
    max_tokens: int | None = None

    @property
    def _llm_type(self) -> str:
        return "agentops-scripted-fake"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def get_num_tokens_from_messages(
        self,
        messages: list[BaseMessage],
        tools: Sequence[Any] | None = None,
    ) -> int:
        """Exactly the input-token count `_generate` will report for these messages and
        tools (the budget ledger reserves with it)."""
        formatted = [convert_to_openai_tool(t) for t in tools or []]
        return estimate_tokens(_prompt_text(messages, formatted))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        turn_idx = turns_since_last_human(messages)
        turn = self.script.get(self.role, self.scenario, turn_idx, self.purpose)
        tools: list[dict[str, Any]] = kwargs.get("tools") or []
        if turn is None:
            last = messages[-1].text if messages else ""
            turn = ScriptedTurn(content=f"[fake:{self.role}] {last[:200]}")
        if turn.error:
            raise FakeProviderError(turn.error)

        tool_calls: list[dict[str, Any]] = []
        if turn.structured is not None:
            if not tools:
                raise ValueError("scripted structured output requires a bound schema tool")
            name = tools[0]["function"]["name"]
            tool_calls = [{"name": name, "args": turn.structured}]
        else:
            tool_calls = list(turn.tool_calls)
        calls = [
            {
                "name": c["name"],
                "args": c["args"],
                "id": f"call_{turn_idx}_{i}",
                "type": "tool_call",
            }
            for i, c in enumerate(tool_calls)
        ]
        out_text = turn.content + json.dumps(tool_calls, sort_keys=True)
        in_tok = estimate_tokens(_prompt_text(messages, tools))
        out_tok = estimate_tokens(out_text)
        if self.max_tokens is not None:
            out_tok = min(out_tok, self.max_tokens)
        msg = AIMessage(
            content=turn.content,
            tool_calls=calls,
            usage_metadata={
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
            },
            response_metadata={"model_name": self.model_name},
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])
