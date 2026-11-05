"""ModelClient: the only path from agents to a chat model (ADR-009).

Per attempt (including retries and structured-output re-asks):
  1. budget check BEFORE the call (raises BudgetExceededError, no call made)
  2. call the model
  3. record an `llm_calls` row: tokens, cost (versioned price table), latency, ok/error
  4. charge the budget
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentops.core.budget import BudgetState
from agentops.db.models import LLMCall
from agentops.models import pricing
from agentops.models.fake import FakeScript, ScriptedChatModel
from agentops.settings import LLMMode, RoleModel, Settings

ChatFactory = Callable[[str, RoleModel], BaseChatModel]


class ModelCallError(Exception):
    """All attempts failed."""


@dataclass(frozen=True)
class ModelResult:
    message: AIMessage
    parsed: BaseModel | None
    budget: BudgetState
    attempts: int


def fake_factory(script: FakeScript | None = None, scenario: str = "default") -> ChatFactory:
    def make(role: str, cfg: RoleModel) -> BaseChatModel:
        return ScriptedChatModel(
            role=role, scenario=scenario, script=script or FakeScript(), model_name=cfg.model
        )

    return make


def _digest(messages: Sequence[BaseMessage]) -> str:
    raw = json.dumps([[m.type, m.text] for m in messages], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class ModelClient:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        chat_factory: ChatFactory | None = None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if chat_factory is None:
            if settings.llm_mode is not LLMMode.FAKE:
                raise NotImplementedError("real model factory is added in Phase 2")
            chat_factory = fake_factory()
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._factory = chat_factory
        self._sleep = sleep

    async def invoke(
        self,
        role: str,
        messages: Sequence[BaseMessage],
        *,
        budget: BudgetState,
        task_id: uuid.UUID | None = None,
        tools: Sequence[Any] | None = None,
        schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        cfg = self._settings.model_for(role)
        model = self._factory(role, cfg)
        convo = list(messages)
        max_attempts = 1 + self._settings.llm_max_retries
        last_error: str | None = None

        for attempt in range(1, max_attempts + 1):
            budget.check()
            runnable: Any
            if schema is not None:
                runnable = model.with_structured_output(schema, include_raw=True)
            elif tools:
                runnable = model.bind_tools(list(tools))
            else:
                runnable = model
            started = time.perf_counter()
            raw: AIMessage | None = None
            parsed: BaseModel | None = None
            error: str | None = None
            try:
                out = await runnable.ainvoke(convo)
                if schema is not None:
                    raw = out["raw"]
                    parsed = out["parsed"]
                    if out.get("parsing_error") is not None or parsed is None:
                        error = f"structured output invalid: {out.get('parsing_error')}"
                else:
                    raw = out
            except Exception as exc:  # provider/transport errors are retried
                error = f"{type(exc).__name__}: {exc}"
            latency_ms = int((time.perf_counter() - started) * 1000)

            usage: dict[str, Any] = dict(raw.usage_metadata or {}) if raw is not None else {}
            in_tok = int(usage.get("input_tokens", 0))
            out_tok = int(usage.get("output_tokens", 0))
            cost = pricing.cost_usd(cfg.provider, cfg.model, in_tok, out_tok)
            await self._record(
                task_id=task_id,
                role=role,
                cfg=cfg,
                attempt=attempt,
                ok=error is None,
                error=error,
                in_tok=in_tok,
                out_tok=out_tok,
                cost=cost,
                latency_ms=latency_ms,
                digest=_digest(convo),
                response=raw,
            )
            budget = budget.charge(tokens=in_tok + out_tok, usd=cost)

            if error is None and raw is not None:
                return ModelResult(message=raw, parsed=parsed, budget=budget, attempts=attempt)

            last_error = error
            if attempt < max_attempts:
                if schema is not None and raw is not None:
                    # structured-output re-ask: show the model its invalid answer and the error
                    convo = [
                        *messages,
                        raw,
                        HumanMessage(f"Your previous answer was invalid: {error}. Answer again."),
                    ]
                await self._sleep(self._settings.llm_retry_base_seconds * 2 ** (attempt - 1))

        raise ModelCallError(f"{role}: all {max_attempts} attempts failed; last: {last_error}")

    async def _record(
        self,
        *,
        task_id: uuid.UUID | None,
        role: str,
        cfg: RoleModel,
        attempt: int,
        ok: bool,
        error: str | None,
        in_tok: int,
        out_tok: int,
        cost: float | None,
        latency_ms: int,
        digest: str,
        response: AIMessage | None,
    ) -> None:
        resp = None
        if response is not None:
            resp = {"content": response.text, "tool_calls": response.tool_calls}
        async with self._sessionmaker() as s, s.begin():
            s.add(
                LLMCall(
                    task_id=task_id,
                    role=role,
                    provider=cfg.provider,
                    model=cfg.model,
                    attempt=attempt,
                    ok=ok,
                    error=error[:2000] if error else None,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                    price_table_version=pricing.PRICE_TABLE_VERSION,
                    latency_ms=latency_ms,
                    request_digest=digest,
                    response=resp,
                )
            )
