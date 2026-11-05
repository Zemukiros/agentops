"""Deterministic scripted chat model for LLM_MODE=fake (ADR-009).

Determinism without hidden state: the response is chosen by (role, scenario, turn), where
`turn` = number of AI messages already in the conversation. The same input therefore always
produces the same output, in any process. That property is what lets a resumed run on a
different worker see exactly what the first worker saw.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
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
    """turns[(role, scenario)] = ordered list of responses by turn index."""

    turns: dict[str, list[ScriptedTurn]] = Field(default_factory=dict)

    @staticmethod
    def key(role: str, scenario: str) -> str:
        return f"{role}:{scenario}"

    def add(self, role: str, scenario: str, *turns: ScriptedTurn) -> FakeScript:
        self.turns.setdefault(self.key(role, scenario), []).extend(turns)
        return self

    def get(self, role: str, scenario: str, turn: int) -> ScriptedTurn | None:
        seq = self.turns.get(self.key(role, scenario)) or self.turns.get(self.key(role, "*"))
        if not seq:
            return None
        return seq[min(turn, len(seq) - 1)]


class FakeProviderError(RuntimeError):
    """Raised for scripted failures; ModelClient treats it as retryable."""


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token). Fake mode only."""
    return max(1, (len(text) + 3) // 4)


class ScriptedChatModel(BaseChatModel):
    role: str
    scenario: str = "default"
    script: FakeScript = Field(default_factory=FakeScript)
    model_name: str = "fake-scripted-v1"

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

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        turn_idx = sum(1 for m in messages if isinstance(m, AIMessage))
        turn = self.script.get(self.role, self.scenario, turn_idx)
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
        prompt_text = "".join(m.text for m in messages) + json.dumps(tools, sort_keys=True)
        out_text = turn.content + json.dumps(tool_calls, sort_keys=True)
        in_tok, out_tok = estimate_tokens(prompt_text), estimate_tokens(out_text)
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
